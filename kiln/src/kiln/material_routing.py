"""Smart material routing for 3D printing.

Translates user intent ("make it strong", "make it pretty", "make it cheap")
into optimal material + settings combinations based on historical data,
material properties, and printer capabilities.
"""

from __future__ import annotations

import logging
import re
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
    availability: dict[str, Any] | None = None  # on-hand attribution, if asked

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


def _format_nozzle_context_line(
    summary: dict[str, Any] | None,
) -> str:
    """Format a one-sentence nozzle context line for the reasoning string.

    The line names the active nozzle material, diameter, provenance,
    approximate grams-through, and an optional low-confidence hedge.
    Capped at ~150 chars.  Returns an empty string when ``summary`` is
    ``None`` or missing required fields so the caller can append
    unconditionally.

    Example output:
        "Nozzle context: brass nozzle (0.4mm, provenance: bambu_mqtt, ~120g through). Settings tuned accordingly."
    """
    if not summary or not isinstance(summary, dict):
        return ""
    material = summary.get("material")
    if not material:
        return ""

    parts: list[str] = []
    diameter = summary.get("diameter_mm")
    if isinstance(diameter, (int, float)) and diameter > 0:
        parts.append(f"{float(diameter):g}mm")
    provenance = summary.get("provenance")
    if provenance:
        parts.append(f"provenance: {provenance}")
    grams = summary.get("grams_through")
    if isinstance(grams, (int, float)) and grams > 0:
        if grams >= 1000:
            parts.append(f"~{grams / 1000:.1f}kg through")
        else:
            parts.append(f"~{int(round(grams))}g through")

    detail = f" ({', '.join(parts)})" if parts else ""
    line = (
        f"Nozzle context: {material} nozzle{detail}. "
        f"Settings tuned accordingly."
    )
    if summary.get("trusted_for_verdicts") is False:
        line = (
            f"Nozzle context: {material} nozzle{detail} "
            f"(low confidence — replace if you've swapped without updating Kiln)."
        )
    # Defensive cap so a future bridge change can't blow the line up.
    return line if len(line) <= 200 else line[:197] + "..."


# Aliases from inventory material strings to catalog names.  Keys are
# lowercase inventory tokens; values are catalog keys in ``_MATERIALS``.
_CATALOG_ALIASES: dict[str, str] = {
    "pla+": "pla_plus",
    "pla plus": "pla_plus",
    "pa": "nylon",
    "polyamide": "nylon",
    "polycarbonate": "pc",
}


def _catalog_match(material_type: str) -> str | None:
    """Map an inventory material string to a catalog material name.

    Inventory strings come from AMS sync and user spools ("PETG-CF",
    "PLA+", "PA-CF"); the catalog keys are base families ("petg",
    "pla_plus", "nylon").  Filled variants (CF/GF/…) match their base
    family for candidacy; the caller keeps the exact inventory string
    for attribution, so a response can still say "PETG-CF".  Returns
    ``None`` when nothing in the catalog corresponds.
    """
    token = (material_type or "").strip().lower()
    if not token:
        return None
    for candidate in (token, token.replace("-", "_").replace(" ", "_")):
        if candidate in _MATERIALS:
            return candidate
        if candidate in _CATALOG_ALIASES:
            return _CATALOG_ALIASES[candidate]
    base = re.split(r"[-_ ]", token, maxsplit=1)[0]
    if base in _MATERIALS:
        return base
    return _CATALOG_ALIASES.get(base)


def _availability_for(
    catalog_name: str,
    on_hand_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build the on-hand attribution block for one catalog material.

    Says WHERE the material physically is: which machines have it loaded
    (with the exact inventory string, so "PETG-CF" survives the catalog
    mapping) and which unloaded shelf spools carry it.
    """
    loaded: list[dict[str, Any]] = []
    shelf: list[dict[str, Any]] = []
    as_recorded: list[str] = []
    for entry in on_hand_index.get(catalog_name, []):
        mt = str(entry.get("material_type", ""))
        if mt and mt not in as_recorded:
            as_recorded.append(mt)
        for row in entry.get("loaded_on") or []:
            loaded.append({**row, "material_type": mt})
        for sp in entry.get("shelf_spools") or []:
            shelf.append({**sp, "material_type": mt})
    # Name the machine with the most material first (the one to use),
    # matching find_printers_with_material's most-stock-first ordering.
    loaded.sort(
        key=lambda r: r.get("remaining_grams") or 0.0, reverse=True
    )
    status = "loaded" if loaded else "on_shelf"
    return {
        "status": status,
        "as_recorded": as_recorded,
        "loaded_on": loaded,
        "shelf_spools": shelf,
        "swap_needed": status == "on_shelf",
    }


def _format_on_hand_line(availability: dict[str, Any]) -> str:
    """One honest sentence saying where the recommended material is."""
    names = ", ".join(availability.get("as_recorded", [])) or "material"
    if availability["status"] == "loaded":
        spots = []
        for row in availability["loaded_on"][:3]:
            grams = row.get("remaining_grams")
            where = row.get("printer_name", "?")
            spots.append(
                f"{where} (~{grams:.0f}g)" if isinstance(grams, (int, float))
                else where
            )
        return f"ON HAND: {names} loaded on {', '.join(spots)}."
    count = len(availability.get("shelf_spools", []))
    return (
        f"ON HAND (shelf only): {count} spool(s) of {names} available but "
        f"not currently loaded — a spool swap is needed before printing "
        f"(suggest_spool_swaps can plan it)."
    )


def recommend_material(
    intent: str,
    *,
    printer_capabilities: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    model_fingerprint: dict[str, Any] | None = None,
    printer_id: str = "",
    on_hand: list[dict[str, Any]] | None = None,
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
    :param printer_id: Optional active-printer identifier.  When supplied
        AND kiln-pro is installed, the function consults the printer's
        nozzle state via ``_pro_nozzle_bridge``.  Two enrichments fire,
        independently and silently when kiln-pro is absent:

        1. **Abrasive escalation.** If the top recommendation is an
           abrasive material (CF / GF / wood / metal fill) AND the
           active nozzle is brass, the reasoning gains a prepended
           advisory line warning of the short brass lifetime (e.g.
           "PETG-CF on brass burns through ~360 g before catastrophic
           tip wear").

        2. **Nozzle context.** Regardless of abrasive-ness, a single
           sentence is appended to the reasoning naming the active
           nozzle material, diameter, provenance, and approximate
           grams-through, so the caller sees which nozzle the
           recommendation was computed against.  An untrusted-state
           hedge is appended when ``trusted_for_verdicts`` is False.

        Free-tier installs without kiln-pro silently skip both
        enrichments — reasoning is unchanged.
    :param on_hand: Optional on-hand inventory (each entry the
        ``OnHandMaterial.to_dict()`` shape from
        :func:`kiln.material_inventory.get_on_hand_materials`).  When
        provided, candidacy narrows to materials the caller physically
        has, and the recommendation carries an ``availability`` block
        attributing the answer to the machine(s) holding it (or to the
        shelf, when a spool swap is needed first).  When nothing on hand
        matches the catalog, the best CATALOG pick is returned, clearly
        labeled needs-purchase — never a silent widening.
    """
    mapping = parse_intent(intent)
    candidates = list(_MATERIALS.values())

    # On-hand narrowing: when the caller supplied their physical
    # inventory, the candidate universe is what they actually have.
    # Entries with neither a loaded row nor a shelf spool carry no
    # physical material and confer no candidacy.
    on_hand_index: dict[str, list[dict[str, Any]]] = {}
    on_hand_status: str | None = None
    if on_hand is not None:
        for entry in on_hand:
            if not (entry.get("loaded_on") or entry.get("shelf_spools")):
                continue
            cat = _catalog_match(str(entry.get("material_type", "")))
            if cat is not None:
                on_hand_index.setdefault(cat, []).append(entry)
        matched = [m for m in candidates if m.name in on_hand_index]
        if matched:
            candidates = matched
            on_hand_status = "on_hand"
        elif not on_hand:
            on_hand_status = "no_inventory_recorded"
        else:
            on_hand_status = "needs_purchase"

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

    # Food-safety overlay (kiln-pro feature; free-tier silently skips):
    # when the user's intent implies food / pet / mouth contact, drop
    # candidates whose chemical.food_safe rating is "no".  Conditional
    # materials (PLA) stay in but rank below food_safe=yes options
    # because the scorer doesn't know about food-safety on its own.
    try:
        from kiln_pro.material_safety import (  # noqa: WPS433
            filter_materials_by_food_safety,
            use_case_implies_food_contact,
        )
    except ImportError:
        filter_materials_by_food_safety = None  # type: ignore[assignment]
        use_case_implies_food_contact = None  # type: ignore[assignment]
    if (
        use_case_implies_food_contact is not None
        and use_case_implies_food_contact(intent)
    ):
        names = [getattr(m, "name", str(m)) for m in candidates]
        safe_set = set(
            filter_materials_by_food_safety(names, require="yes_or_conditional")
        )
        food_filtered = [
            m for m in candidates
            if getattr(m, "name", str(m)) in safe_set
        ]
        if food_filtered:
            candidates = food_filtered

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

    # On-hand attribution — say WHERE the answer physically is, or say
    # out loud that it isn't on hand.  Never silently widen: the
    # needs-purchase fallback keeps the catalog answer but labels it.
    availability: dict[str, Any] | None = None
    if on_hand_status == "on_hand":
        availability = _availability_for(top_mat.name, on_hand_index)
        reasoning = f"{reasoning} {_format_on_hand_line(availability)}"
    elif on_hand_status is not None:
        recorded = sorted({
            str(e.get("material_type", "")) for e in (on_hand or [])
            if e.get("material_type")
        })
        availability = {
            "status": on_hand_status,
            "as_recorded": [],
            "loaded_on": [],
            "shelf_spools": [],
            "swap_needed": False,
            "on_hand_recorded": recorded,
        }
        if on_hand_status == "no_inventory_recorded":
            reasoning = (
                "NOT ON HAND: no spools or loaded materials are recorded — "
                "record inventory with add_spool or an AMS/CFS sync to get "
                "on-hand answers. Catalog recommendation follows; purchase "
                f"required.\n\n{reasoning}"
            )
        else:
            have = ", ".join(recorded) or "your recorded materials"
            reasoning = (
                f"NOT ON HAND: nothing you have recorded ({have}) matches "
                f"this request — {top_mat.display_name} is a catalog pick "
                f"and needs to be purchased.\n\n{reasoning}"
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
        alt: dict[str, Any] = {
            "material": alt_mat.display_name,
            "name": alt_mat.name,
            "score": alt_score,
            "settings": _default_settings(alt_mat),
        }
        if on_hand_status == "on_hand":
            alt_avail = _availability_for(alt_mat.name, on_hand_index)
            alt["availability"] = {
                "status": alt_avail["status"],
                "loaded_on": [
                    r.get("printer_name") for r in alt_avail["loaded_on"]
                ],
                "shelf_spools": len(alt_avail["shelf_spools"]),
            }
        alternatives.append(alt)

    # Nozzle overlays — when the caller supplied a printer_id AND
    # kiln-pro is installed, consult the bridge for (a) abrasive
    # escalation against the top pick (prepended NOZZLE ADVISORY) and
    # (b) a one-sentence nozzle-context line (appended after the
    # reasoning).  Both surface as reasoning-string mutations to
    # preserve the existing return shape for callers that parse it.
    # Free-tier installs without kiln-pro silently skip both wires.
    if printer_id:
        try:
            from kiln import _pro_nozzle_bridge

            # Consult with the material the user will ACTUALLY print:
            # in on-hand mode that's the recorded inventory string
            # ("PETG-CF"), whose abrasive-ness the catalog base name
            # ("petg") would understate.
            _filament = top_mat.name
            if availability and availability.get("as_recorded"):
                _filament = availability["as_recorded"][0]
            _nozzle_advisory = _pro_nozzle_bridge.consult_abrasive_escalation(
                filament_material=_filament,
                printer_id=printer_id,
            )
            if (
                _nozzle_advisory is not None
                and _nozzle_advisory.get("escalation_reason") == "abrasive_brass"
            ):
                reasoning = (
                    f"NOZZLE ADVISORY: {_nozzle_advisory.get('user_warning', '')} "
                    f"\n\n{reasoning}"
                )

            _nozzle_summary = _pro_nozzle_bridge.consult_nozzle_summary(
                printer_id,
            )
            _nozzle_line = _format_nozzle_context_line(_nozzle_summary)
            if _nozzle_line:
                reasoning = f"{reasoning} | {_nozzle_line}"
        except Exception:
            logger.debug(
                "Nozzle overlay skipped", exc_info=True,
            )

    # Skin-contact advisory (worn / handled against skin).  This is NOT a
    # filter — no printed material is skin-safe, so we never drop or downrank
    # a candidate (unlike the food-safe filter above).  When the intent implies
    # a worn item we prepend the honest caution for the chosen material so a
    # material picked for a ring or band carries it.  Public floor; reaches
    # every install.  Advisory only, best-effort, never breaks routing.
    try:
        from kiln.design_intelligence import (
            get_skin_contact_floor,
            use_case_implies_skin_contact,
        )

        if use_case_implies_skin_contact(intent):
            _floor = get_skin_contact_floor(top_mat.name)
            _note = _floor.honesty_note if _floor is not None else ""
            reasoning = (
                "SKIN CONTACT: no 3D-printed part is skin-safe, hypoallergenic, "
                f"or biocompatible. {_note} For any mouth, eye, broken-skin, "
                "piercing, or implant use, see a medical professional.\n\n"
                f"{reasoning}"
            )
    except Exception:  # noqa: BLE001 — advisory is best-effort; never break routing
        logger.debug("Skin-contact advisory skipped", exc_info=True)

    return MaterialRecommendation(
        material=top_mat,
        score=top_score,
        reasoning=reasoning,
        settings=_default_settings(top_mat),
        estimated_cost_usd=top_mat.cost_per_kg_usd,
        success_rate=success_rate,
        alternatives=alternatives,
        availability=availability,
    )


def list_materials() -> list[MaterialProperties]:
    """Return all available materials sorted by name."""
    return sorted(_MATERIALS.values(), key=lambda m: m.name)


def get_material(name: str) -> MaterialProperties | None:
    """Look up a material by name.

    :param name: Material name (case-insensitive).
    """
    return _MATERIALS.get(name.lower())
