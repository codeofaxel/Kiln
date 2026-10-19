"""Material compatibility, process settings, and purge volume recommendations.

Used by the multi-color/multi-material 3MF composer to:

1. **Warn** when incompatible materials are paired (e.g., PLA + ASA).
2. **Recommend** purge volumes between each material pair.
3. **Suggest** per-extruder process settings (temp, speed) for known materials.
4. **Embed** a BambuStudio-compatible flush volume matrix in the 3MF.

Safety philosophy
-----------------
* Warnings are *always* free tier — users must know about risks.
* Hard blocks are only issued for pairings that are genuinely dangerous
  (extreme temp deltas, reactive chemistries, hardware damage risk).
* Conditional pairings return warnings + recommended mitigations, not blocks.
* Smart auto-selection of optimal settings for exotic combos → kiln-pro.

Compatibility levels
--------------------
* ``"ok"``          — print freely, no special precautions needed.
* ``"caution"``     — works, but read the warning before printing.
* ``"conditional"`` — possible with specific mitigations (interface layers, etc.).
* ``"incompatible"``— do not attempt; likely to fail or damage hardware.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Food contact and the nozzle it came through
# ---------------------------------------------------------------------------
#
# Choosing a food-safe filament settles the polymer and nothing else.  The
# part is extruded through the nozzle, and a standard nozzle is brass — most
# commonly the free-machining alloy that carries a small percentage of lead
# for machinability.  A little of that tip wears into the print over the
# nozzle's life, so a bowl in a food-safe polymer printed through a leaded
# nozzle is not a food-safe bowl.
#
# This is stated as a hardware fact and a substitution, not a verdict: Kiln
# free does not know which nozzle is fitted or how worn it is, and lead-free
# brass nozzles do exist.  Saying "usually" is the honest form — the reader
# can check their own nozzle, and the swap is cheap either way.
#
# Deliberately free.  Whether a given material and nozzle combination clears
# a hard food-contact bar is a paid verdict; knowing that the nozzle is part
# of the question at all is not something to learn after eating off the part.

#: Advisory for any recommendation whose use case implies food contact.
FOOD_CONTACT_NOZZLE_ADVISORY: str = (
    "A food-safe filament is only half of it — the part is extruded through "
    "the nozzle, and standard nozzles are usually a leaded brass alloy that "
    "wears trace lead into the print over its life. For anything a person or "
    "an animal will eat or drink from, print it through a hardened steel or "
    "stainless nozzle, and check what yours is made of."
)


def food_contact_nozzle_advisory() -> str:
    """The nozzle advisory that belongs with every food-contact answer.

    One source, so each door that concludes food contact says the same
    thing.  Kept as a function rather than a bare constant because the
    callers are in different modules and a shared import is the seam that
    keeps them from drifting into three slightly different warnings.
    """
    return FOOD_CONTACT_NOZZLE_ADVISORY


# ---------------------------------------------------------------------------
# Food contact: the rating and the detector every install carries
# ---------------------------------------------------------------------------
#
# Whether a printed part will hold food, touch a mouth, or feed a pet decides
# which materials may be recommended for it at all.  That is a safety floor,
# so it lives here, in the public package, and answers the same way on every
# install whether or not anything else is present.  There is ONE rating per
# material, ``chemical.food_safe`` in ``data/design_knowledge/materials.json``;
# anything layered on top (product classes, served depth) adds conditions and
# never contradicts it.
#
# The recommendation rule is deliberately conservative in one direction:
# ``"no"`` is dropped, ``"yes"`` and ``"conditional"`` stay.  A conditional
# material keeps its conditions in the warning text and is never described as
# food safe.

#: Words and phrases that put a printed part against food, drink, the mouth,
#: or a pet's bowl.  Wide on purpose: an unneeded match narrows the material
#: list, while a miss can hand someone a ``food_safe="no"`` material for a
#: bowl.  Matching is whole-word, and hyphens and underscores read as spaces,
#: so "pet_bowl", "pet-bowl" and "pet bowl" are one phrase.  Bare "glass" and
#: "bottle" are absent because glass-filled nylon and a spray bottle are not
#: food; the drinkware phrases carry those.
FOOD_CONTACT_TOKENS: frozenset[str] = frozenset({
    # Direct statements of intent
    "food", "food safe", "foodsafe", "food contact", "food grade",
    "edible", "consumable", "ingested",
    # Eating and drinking
    "eat", "eating", "dining", "meal", "lunch", "lunchbox", "lunch box",
    "snack", "breakfast", "dinner", "picnic", "bento", "cereal",
    "drink", "drinking", "beverage",
    # Drinkware
    "cup", "mug", "tumbler", "stein", "goblet", "flask", "thermos",
    "carafe", "pitcher", "straw", "sippy cup", "drinking glass",
    "wine glass", "shot glass", "water bottle", "drink bottle",
    "baby bottle", "coffee", "tea", "teapot", "kettle", "juice", "smoothie",
    # Serving and preparation
    "kitchen", "bowl", "plate", "platter", "dish", "saucer", "ramekin",
    "tray", "utensil", "cutlery", "silverware", "spoon", "fork",
    "chopstick", "skewer", "spatula", "ladle", "whisk", "scoop",
    "colander", "strainer", "cutting board", "chopping board",
    "measuring cup", "measuring spoon", "trivet", "placemat",
    # Baking and cookery
    "cookie", "cookie cutter", "baking", "pastry", "dough", "bread",
    "cake", "icing", "fondant", "chocolate", "candy",
    # Pantry and storage
    "food container", "food storage", "spice", "spice rack", "salt",
    "pepper", "sugar", "honey", "canister", "jar lid", "mason jar",
    # Ice
    "ice", "iced", "ice cube", "ice tray", "ice cream", "popsicle",
    # Mouth and oral contact
    "mouth", "mouthpiece", "mouthguard", "mouth guard", "oral",
    "tooth", "teeth", "toothbrush", "toothpick", "dental", "denture",
    "pacifier", "teether", "whistle", "mouthwash",
    # Pets: a pet bowl is a food bowl
    "pet bowl", "dog bowl", "cat bowl", "food bowl", "water bowl",
    "feeder", "feeding", "pet treat", "treat dispenser",
})

#: A word directly in front of one of the qualifier-sensitive nouns below that
#: marks its hardware sense: a "top plate" is structure, a "suction cup" is a
#: foot, "cable feeding" is routing, a "pen cup" is one of Kiln's own
#: generators.  A bare or food-qualified noun still fires, so "a dinner plate
#: that sits on the build plate" is food contact and "the build plate" alone
#: is not.  Qualifying the noun beats suppressing the brief, which would
#: silence a real bowl.
FOOD_MECHANICAL_QUALIFIERS: frozenset[str] = frozenset({
    "top", "bottom", "base", "back", "front", "side", "wall", "face",
    "cover", "mounting", "mount", "adapter", "adaptor", "license",
    "number", "build", "kick", "switch", "blanking", "gusset",
    "backing", "steel", "metal", "aluminum", "aluminium", "vesa",
    "suction", "bearing", "cable", "wire", "filament", "paper",
    "bed", "print", "parts", "part", "paint", "screw", "screws", "tool",
    "tools", "desk", "drip", "sorting", "jewelry", "jewellery", "component",
    "battery", "seed", "air", "dust", "ash",
    "pen", "pencil", "brush", "soap", "rolling", "satellite",
})

#: The nouns whose sense the qualifier in front of them settles.
_FOOD_QUALIFIER_SENSITIVE: frozenset[str] = frozenset({
    "plate", "cup", "feeding", "tray", "scoop", "dish",
})

#: The recommendation bar: a candidate stays when its rating is one of these.
FOOD_SAFE_KEEP: dict[str, frozenset[str]] = {
    "yes": frozenset({"yes"}),
    "yes_or_conditional": frozenset({"yes", "conditional"}),
}


def _fold_separators(text: str) -> str:
    """Lowercase and read hyphens and underscores as spaces."""
    return re.sub(r"[_\-]+", " ", text.lower())


def _whole_word_alternation(tokens: frozenset[str]) -> re.Pattern[str]:
    """One pattern matching any token as whole words, plural suffix allowed.

    A phrase tolerates any run of whitespace between its words, so a wrapped
    line still reads "cutting board" as one phrase.
    """
    alternatives = [
        r"\s+".join(re.escape(word) + r"(?:e?s)?" for word in token.split())
        for token in sorted(tokens, key=len, reverse=True)
    ]
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b")


@lru_cache(maxsize=1)
def _food_contact_pattern() -> re.Pattern[str]:
    """The trigger vocabulary compiled once, with design intelligence's own
    food keywords folded in so a word added there reaches this detector too.

    Built on first use so importing this module never imports
    ``design_intelligence``.
    """
    tokens = {_fold_separators(t) for t in FOOD_CONTACT_TOKENS}
    from kiln.design_intelligence import _FOOD_KEYWORDS

    tokens.update(_fold_separators(str(k)) for k in _FOOD_KEYWORDS)
    return _whole_word_alternation(frozenset(tokens))


@lru_cache(maxsize=1)
def _food_sensitive_forms() -> frozenset[str]:
    """The qualifier-sensitive nouns plus the plurals the pattern can match."""
    return frozenset(
        form
        for base in _FOOD_QUALIFIER_SENSITIVE
        for form in (base, base + "s", base + "es")
    )


def use_case_implies_food_contact(use_case: str) -> bool:
    """True when a brief suggests the part meets food, drink, a mouth or a pet.

    Whole words only, so *slice*, *device* and *cupboard* are not food.  See
    :data:`FOOD_MECHANICAL_QUALIFIERS` for how "build plate" and "pen cup" are
    told apart from a dinner plate and a coffee cup.
    """
    if not isinstance(use_case, str) or not use_case.strip():
        return False
    text = _fold_separators(use_case)
    sensitive = _food_sensitive_forms()
    for match in _food_contact_pattern().finditer(text):
        if match.group(0) in sensitive and _word_before(text, match.start()) in FOOD_MECHANICAL_QUALIFIERS:
            continue
        return True
    return False


def _word_before(text: str, end: int) -> str:
    """The run of letters ending just before *end*, skipping whitespace.

    A backward scan rather than a regex over ``text[:end]``: that regex
    backtracks across the whole prefix at every match, which grows with the
    square of a long brief.
    """
    stop = end
    while stop > 0 and text[stop - 1].isspace():
        stop -= 1
    start = stop
    while start > 0 and "a" <= text[start - 1] <= "z":
        start -= 1
    return text[start:stop]


def _materials_json_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "design_knowledge" / "materials.json"


@lru_cache(maxsize=1)
def _food_ratings() -> dict[str, str]:
    """``{catalog key: food_safe rating}`` read from the public catalog.

    Read straight from the file rather than through the design-intelligence
    loader, because that loader merges served depth on top and this rating is
    the one every install must agree on.  A catalog that cannot be read gives
    an empty map, which every caller below treats as "not rated": refused for
    food contact, never assumed safe.
    """
    try:
        data = json.loads(_materials_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ratings: dict[str, str] = {}
    for key, entry in data.items():
        if key.startswith("_") or not isinstance(entry, dict):
            continue
        rating = str((entry.get("chemical") or {}).get("food_safe") or "").lower()
        if rating:
            ratings[key] = rating
    return ratings


def food_catalog_key(material: str) -> str:
    """A material name in the catalog's key form: ``"PETG-HF"`` -> ``"petg_hf"``,
    ``"PLA+"`` -> ``"pla_plus"``."""
    if not isinstance(material, str):
        return ""
    key = material.strip().lower().replace("+", "_plus")
    return re.sub(r"[\s\-]+", "_", key)


def food_safe_rating(material: str) -> str | None:
    """The catalog's ``food_safe`` rating for *material*, or ``None`` when the
    material is not in the catalog (which callers treat as not food safe)."""
    return _food_ratings().get(food_catalog_key(material))


def filter_food_safe_materials(
    candidates: list[str], require: str = "yes"
) -> list[str]:
    """The candidates whose rating clears the bar, in their given order and
    spelling.

    :param require: ``"yes"`` keeps only food-safe ratings;
        ``"yes_or_conditional"`` drops only ``"no"`` (and anything unrated).
    """
    keep = FOOD_SAFE_KEEP.get(require)
    if keep is None:
        raise ValueError(f"require must be one of {sorted(FOOD_SAFE_KEEP)}")
    return [c for c in candidates if food_safe_rating(c) in keep]


def approved_food_safe_materials() -> list[str]:
    """Catalog keys rated ``food_safe="yes"``, computed from the catalog so a
    newly rated material joins without a code change."""
    return sorted(k for k, v in _food_ratings().items() if v == "yes")


def _food_display(key: str) -> str:
    """``"petg_hf"`` -> ``"PETG-HF"``, for prose only."""
    return key.upper().replace("_", "-")


def _food_display_csv(keys: list[str]) -> str:
    return ", ".join(_food_display(k) for k in keys)


_FOOD_REFUSE = (
    "Material '{material}' is rated not food safe in Kiln's material catalog. "
    "It is not safe for food, drink, mouth or pet contact in any condition: "
    "it can off-gas, leach or shed into whatever touches it. Use one of the "
    "food-safe options instead: {approved}."
)

_FOOD_CONDITIONAL = (
    "Material '{material}' is food safe only under conditions: water only (no "
    "acidic, fatty or hot food), hand wash with mild soap in warm (not hot) "
    "water, and replace it every 6-12 months as the layer lines wear. If any "
    "of those does not fit, use {approved} instead."
)

_FOOD_APPROVED = (
    "Material '{material}' is rated food safe in Kiln's material catalog. An "
    "FDM print is still not certified for food contact even in a food-safe "
    "filament: the gaps between layers hold bacteria over time. Hand wash, no "
    "dishwasher, replace it yearly."
)

_FOOD_UNKNOWN = (
    "Material '{material}' is not in Kiln's material catalog, so it is treated "
    "as unsafe for food, drink, mouth or pet contact."
)


def assess_food_safety(material: str) -> dict[str, Any]:
    """The food-contact verdict for one material.

    ``verdict`` is ``"approved"`` (rated yes, with the not-certified caveat),
    ``"warn"`` (conditional: the conditions are in ``user_warning``) or
    ``"refuse"`` (rated no, or not in the catalog).  ``approved_alternatives``
    lists the catalog keys rated yes.
    """
    key = food_catalog_key(material)
    rating = _food_ratings().get(key)
    approved = approved_food_safe_materials()
    name = _food_display(material) if isinstance(material, str) else str(material)
    if rating == "yes":
        verdict, message = "approved", _FOOD_APPROVED.format(material=name)
    elif rating == "conditional":
        verdict = "warn"
        message = _FOOD_CONDITIONAL.format(
            material=name, approved=_food_display_csv(approved) or "PETG"
        )
    elif rating == "no":
        verdict = "refuse"
        message = _FOOD_REFUSE.format(
            material=name, approved=_food_display_csv(approved) or "(none rated)"
        )
    else:
        verdict, message = "refuse", _FOOD_UNKNOWN.format(material=material)
    return {
        "material": key,
        "found_in_catalog": rating is not None,
        "food_safe": rating,
        "verdict": verdict,
        "user_warning": message,
        "approved_alternatives": approved,
    }


# ---------------------------------------------------------------------------
# Process settings database — per material
# ---------------------------------------------------------------------------

#: Recommended process settings per material type (case-insensitive key).
#: ``temp_range``: (min, max) nozzle °C for reliable extrusion.
#: ``bed_temp``: recommended bed °C.
#: ``speed_factor``: relative speed multiplier vs PLA (1.0 = same as PLA).
#: ``requires_hardened_nozzle``: brass nozzle will wear out fast.
#: ``requires_enclosure``: warping / fume risk without enclosure.
MATERIAL_PROCESS: dict[str, dict[str, Any]] = {
    "PLA": {
        "temp_range": (190, 230),
        "bed_temp": 55,
        "speed_factor": 1.0,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "Most forgiving FDM material. Excellent for multi-color.",
    },
    "PLA-CF": {
        "temp_range": (210, 240),
        "bed_temp": 55,
        "speed_factor": 0.8,
        "requires_hardened_nozzle": True,
        "requires_enclosure": False,
        "notes": "Carbon-fiber PLA. Requires hardened/wear-resistant nozzle.",
    },
    "PLA-HF": {
        "temp_range": (200, 230),
        "bed_temp": 55,
        "speed_factor": 1.2,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "High-flow PLA variant. Generally PLA-compatible.",
    },
    "PETG": {
        "temp_range": (230, 250),
        "bed_temp": 70,
        "speed_factor": 0.8,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "Higher temp than PLA. Sticks to PLA poorly — use interface layer.",
    },
    "TPU": {
        "temp_range": (220, 240),
        "bed_temp": 35,
        "speed_factor": 0.4,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "Flexible. Print slowly. Purge thoroughly; retains previous color.",
    },
    "TPE": {
        "temp_range": (220, 240),
        "bed_temp": 35,
        "speed_factor": 0.35,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "Ultra-flexible. Similar to TPU; even slower speeds recommended.",
    },
    "ABS": {
        "temp_range": (230, 250),
        "bed_temp": 100,
        "speed_factor": 0.9,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "Warps without enclosure. Emits styrene fumes — ventilate.",
    },
    "ASA": {
        "temp_range": (240, 260),
        "bed_temp": 100,
        "speed_factor": 0.85,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "UV-resistant ABS variant. High temp — enclosure required.",
    },
    "NYLON": {
        "temp_range": (250, 270),
        "bed_temp": 70,
        "speed_factor": 0.8,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "Hygroscopic — dry filament before use. High warp risk.",
    },
    "PC": {
        "temp_range": (260, 300),
        "bed_temp": 110,
        "speed_factor": 0.7,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "Polycarbonate. Extreme temps. Most consumer printers cannot reach required bed temp.",
    },
    "PVA": {
        "temp_range": (190, 210),
        "bed_temp": 45,
        "speed_factor": 0.6,
        "requires_hardened_nozzle": False,
        "requires_enclosure": False,
        "notes": "Water-soluble support material. Pairs with PLA. Hygroscopic — store dry.",
    },
    "HIPS": {
        "temp_range": (230, 245),
        "bed_temp": 100,
        "speed_factor": 0.9,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "Limonene-soluble support material for ABS. Enclosure required.",
    },
    "PA": {    # Polyamide / Nylon alias
        "temp_range": (250, 270),
        "bed_temp": 70,
        "speed_factor": 0.8,
        "requires_hardened_nozzle": False,
        "requires_enclosure": True,
        "notes": "See NYLON.",
    },
}

# ---------------------------------------------------------------------------
# Compatibility matrix
# ---------------------------------------------------------------------------

#: Two-material compatibility entries.
#: Keys are frozensets of two material names (order-independent).
#: Values: level ("ok" | "caution" | "conditional" | "incompatible"),
#:         warning message, purge_volume_mm3 (extra purge beyond base),
#:         mitigations (list of actionable tips).
_COMPAT: dict[frozenset, dict[str, Any]] = {
    # -----------------------------------------------------------------------
    # PLA pairings
    # -----------------------------------------------------------------------
    frozenset({"PLA"}): {
        "level": "ok",
        "purge_volume_mm3": 30,
        "warning": None,
        "mitigations": [],
    },
    frozenset({"PLA", "PLA-CF"}): {
        "level": "caution",
        "purge_volume_mm3": 40,
        "warning": "PLA-CF requires a hardened/wear-resistant nozzle. "
                   "Standard brass nozzle will wear quickly when extruding CF filament.",
        "mitigations": [
            "Install a hardened steel or ruby nozzle before printing.",
            "Assign PLA-CF to a dedicated slot — never mix with standard brass nozzle.",
        ],
    },
    frozenset({"PLA", "PLA-HF"}): {
        "level": "ok",
        "purge_volume_mm3": 30,
        "warning": None,
        "mitigations": [],
    },
    frozenset({"PLA", "PETG"}): {
        "level": "conditional",
        "purge_volume_mm3": 70,
        "warning": "PLA and PETG do not adhere well to each other. "
                   "Parts will delaminate at the PLA/PETG interface under any load.",
        "mitigations": [
            "Use as support interface only (PETG support under PLA model — releases cleanly).",
            "Do not rely on PLA/PETG adhesion for structural parts.",
            "Increase purge volume to avoid color contamination.",
        ],
    },
    frozenset({"PLA", "TPU"}): {
        "level": "caution",
        "purge_volume_mm3": 90,
        "warning": "TPU is significantly slower to print than PLA. "
                   "Purge volume must be high to clear flexible residue from the nozzle.",
        "mitigations": [
            "Reduce overall print speed to ≤ 40 mm/s when TPU is active.",
            "Increase flush/purge volume to ≥ 100 mm³ for TPU transitions.",
            "Prime the nozzle before TPU sections to avoid under-extrusion.",
        ],
    },
    frozenset({"PLA", "PVA"}): {
        "level": "ok",
        "purge_volume_mm3": 50,
        "warning": "PVA is water-soluble — store dry between sessions. "
                   "Once dissolved, supports release cleanly from PLA.",
        "mitigations": [
            "Store PVA in a sealed container with desiccant.",
            "Use PVA only for support material, not structural geometry.",
        ],
    },
    frozenset({"PLA", "ABS"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: PLA (200 °C) and ABS (230 °C+) have a ~30 °C temperature gap. "
                   "Printing both at the same temp will either under-extrude ABS or "
                   "thermally degrade PLA. High clog and print failure risk.",
        "mitigations": [
            "Do not pair PLA and ABS in the same print.",
            "Use PETG instead of ABS for a closer temperature profile.",
        ],
    },
    frozenset({"PLA", "ASA"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: PLA (200 °C) and ASA (240–260 °C) have a ~50 °C temperature gap. "
                   "Extreme clog risk. Do not attempt.",
        "mitigations": [
            "Do not pair PLA and ASA.",
        ],
    },
    frozenset({"PLA", "NYLON"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: PLA (200 °C) and Nylon (250–270 °C) are thermally incompatible. "
                   "Nylon will not extrude properly at PLA temps.",
        "mitigations": [
            "Do not pair PLA and Nylon in the same print.",
        ],
    },
    frozenset({"PLA", "PC"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: PC requires 260–300 °C. PLA degrades above 230 °C. "
                   "Cannot share a temperature setting.",
        "mitigations": ["Do not pair PLA and PC."],
    },
    # -----------------------------------------------------------------------
    # PETG pairings
    # -----------------------------------------------------------------------
    frozenset({"PETG"}): {
        "level": "ok",
        "purge_volume_mm3": 40,
        "warning": None,
        "mitigations": [],
    },
    frozenset({"PETG", "TPU"}): {
        "level": "caution",
        "purge_volume_mm3": 80,
        "warning": "Similar temps but thorough purging required. "
                   "TPU residue in PETG causes stringing.",
        "mitigations": [
            "Increase flush volume to ≥ 100 mm³ on TPU→PETG transitions.",
            "Reduce speed during TPU segments.",
        ],
    },
    frozenset({"PETG", "ABS"}): {
        "level": "conditional",
        "purge_volume_mm3": 60,
        "warning": "PETG and ABS have overlapping temperature ranges but "
                   "adhesion between the two is unreliable.",
        "mitigations": [
            "Use as support interface only.",
            "Enclosure required for ABS segments.",
        ],
    },
    frozenset({"PETG", "HIPS"}): {
        "level": "caution",
        "purge_volume_mm3": 60,
        "warning": "HIPS is soluble in Limonene; PETG is not. "
                   "Useful as a dissolvable support for PETG.",
        "mitigations": [
            "Enclosure required for HIPS segments.",
            "Soak completed print in Limonene to dissolve HIPS supports.",
        ],
    },
    # -----------------------------------------------------------------------
    # ABS / ASA pairings
    # -----------------------------------------------------------------------
    frozenset({"ABS"}): {
        "level": "ok",
        "purge_volume_mm3": 40,
        "warning": "Enclosure required for all ABS printing.",
        "mitigations": ["Use enclosed printer. Ventilate fumes."],
    },
    frozenset({"ABS", "ASA"}): {
        "level": "ok",
        "purge_volume_mm3": 50,
        "warning": "Enclosure required. ASA runs slightly hotter than ABS.",
        "mitigations": [
            "Set temperature to the higher of the two profiles.",
            "Ventilate or use HEPA filtration.",
        ],
    },
    frozenset({"ABS", "HIPS"}): {
        "level": "ok",
        "purge_volume_mm3": 50,
        "warning": "Classic combo: ABS model + HIPS support. Enclosure required.",
        "mitigations": [
            "Enclosure required.",
            "Dissolve HIPS supports in Limonene after printing.",
        ],
    },
    frozenset({"ABS", "TPU"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: ABS (230 °C) and TPU (220 °C) temps overlap but "
                   "ABS requires enclosure / high bed temp that will deform TPU sections.",
        "mitigations": ["Do not pair ABS and TPU."],
    },
    frozenset({"ASA"}): {
        "level": "ok",
        "purge_volume_mm3": 40,
        "warning": "Enclosure required for all ASA printing.",
        "mitigations": ["Use enclosed printer."],
    },
    # -----------------------------------------------------------------------
    # TPU / flexible
    # -----------------------------------------------------------------------
    frozenset({"TPU"}): {
        "level": "ok",
        "purge_volume_mm3": 60,
        "warning": None,
        "mitigations": [],
    },
    frozenset({"TPU", "TPE"}): {
        "level": "ok",
        "purge_volume_mm3": 70,
        "warning": None,
        "mitigations": [],
    },
}


def _normalise(material: str) -> str:
    """Normalise material name for lookup (uppercase, strip whitespace)."""
    return material.strip().upper()


def _lookup_compat(mat_a: str, mat_b: str) -> dict[str, Any]:
    """Look up compatibility between two materials.  Returns a default
    "unknown" entry if the pairing isn't in the database."""
    key = frozenset({_normalise(mat_a), _normalise(mat_b)})
    if key in _COMPAT:
        return _COMPAT[key]
    # Unknown pairing — caution by default
    return {
        "level": "caution",
        "purge_volume_mm3": 80,
        "warning": (
            f"Compatibility between {mat_a} and {mat_b} is unknown. "
            "Verify temperature ranges are compatible before printing."
        ),
        "mitigations": [
            f"Check nozzle temp range for both {mat_a} and {mat_b}.",
            "Test with a small print before committing to a full job.",
        ],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_material_compatibility(materials: list[str]) -> dict[str, Any]:
    """Check compatibility between all material pairs in a print job.

    Args:
        materials: List of material names (one per extruder slot, 1-indexed
            positions in the list).  Duplicates and ``None``/empty strings
            are ignored.

    Returns:
        Dict with:

        * ``safe`` (bool) — False if any pair is ``"incompatible"``.
        * ``level`` (str) — worst-case level across all pairs.
        * ``pairs`` (list) — per-pair results with level, warning, mitigations.
        * ``hardware_warnings`` (list) — e.g., hardened nozzle requirements.
        * ``purge_matrix`` (list[list[float]]) — recommended flush volumes
          (mm³) as an N×N matrix; use to configure slicer purge settings.
        * ``message`` (str) — human-readable summary.
    """
    clean = [_normalise(m) for m in materials if m and m.strip()]
    if not clean:
        return {"safe": True, "level": "ok", "pairs": [], "hardware_warnings": [],
                "purge_matrix": [], "message": "No materials specified."}

    n = len(clean)
    pairs: list[dict[str, Any]] = []
    worst_level = "ok"
    level_rank = {"ok": 0, "caution": 1, "conditional": 2, "incompatible": 3}
    hardware_warnings: list[str] = []

    # Check all unique pairs
    seen: set[frozenset] = set()
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            key = frozenset({clean[i], clean[j]})
            if key in seen:
                continue
            seen.add(key)
            compat = _lookup_compat(clean[i], clean[j])
            pairs.append({
                "material_a": clean[i],
                "material_b": clean[j],
                "level": compat["level"],
                "warning": compat.get("warning"),
                "mitigations": compat.get("mitigations", []),
                "purge_volume_mm3": compat.get("purge_volume_mm3", 80),
            })
            if level_rank.get(compat["level"], 0) > level_rank.get(worst_level, 0):
                worst_level = compat["level"]

    # Hardware checks per individual material
    for mat in clean:
        info = MATERIAL_PROCESS.get(mat, {})
        if info.get("requires_hardened_nozzle"):
            msg = f"{mat} requires a hardened/wear-resistant nozzle (not standard brass)."
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)
        if info.get("requires_enclosure"):
            msg = f"{mat} requires an enclosure to prevent warping and fume issues."
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)

    # Build purge matrix (N×N, row = from extruder, col = to extruder)
    purge_matrix: list[list[float]] = []
    for i in range(n):
        row: list[float] = []
        for j in range(n):
            if i == j:
                row.append(0.0)
            else:
                compat = _lookup_compat(clean[i], clean[j])
                row.append(float(compat.get("purge_volume_mm3", 80)))
        purge_matrix.append(row)

    is_safe = worst_level != "incompatible"
    incompatible_pairs = [p for p in pairs if p["level"] == "incompatible"]

    if incompatible_pairs:
        message = (
            "⛔ INCOMPATIBLE MATERIALS: "
            + "; ".join(
                f"{p['material_a']} + {p['material_b']}" for p in incompatible_pairs
            )
            + ". Do not print — see warnings for details."
        )
    elif worst_level == "conditional":
        message = (
            "⚠️  Conditional compatibility. Review mitigations before printing: "
            + "; ".join(p["warning"] for p in pairs if p.get("warning"))
        )
    elif worst_level == "caution":
        message = (
            "⚠️  Printable with precautions. Review warnings before starting."
        )
    else:
        message = "✅ All material pairings are compatible."

    return {
        "safe": is_safe,
        "level": worst_level,
        "pairs": pairs,
        "hardware_warnings": hardware_warnings,
        "purge_matrix": purge_matrix,
        "message": message,
    }


def get_process_settings(material: str) -> dict[str, Any] | None:
    """Return recommended process settings for a material, or None if unknown."""
    return MATERIAL_PROCESS.get(_normalise(material))


# ---------------------------------------------------------------------------
# Abrasive-filament floor
# ---------------------------------------------------------------------------

#: Fill wording in a filled filament's own name.  Whole words, because
#: "CARBON" as a bare substring reads POLYCARBONATE as carbon-filled, and
#: "GLASS" alone reads a sea-glass colourway as glass fibre.  Metal fill
#: is left to the ``FILL`` rule below: "Copper" on a spool is far more
#: often a colour than a filler, but "Copperfill" never is.
_ABRASIVE_FILL_PATTERNS = (
    r"\bCARBON\b",
    r"\bWOOD\b",
    r"\bMETAL\b",
    r"GLASS\s*-?\s*FIB",
    r"FILL",
)

#: Fill suffixes, matched as whole tokens so "PETG" never reads as "GF"
#: and a grade number rides along ("PA6-CF15").
_ABRASIVE_FILL_PREFIXES = ("CF", "GF")

_ABRASIVE_NOZZLE_FLOOR = (
    "ABRASIVE FILAMENT: {name} is a filled filament (carbon, glass, wood "
    "or metal). Filled filament grinds a standard brass nozzle out of "
    "round, which shows up as sizes drifting and gaps in the walls long "
    "before the nozzle looks damaged. Kiln cannot see which nozzle is "
    "fitted here, so treat this as a caution and not a measurement: "
    "check yours, and print this on hardened steel if it is brass."
)


def is_abrasive_filament(material: str) -> bool:
    """True when *material* names a filled — and so abrasive — filament.

    Two public sources, no curated table required:

    1. an explicit ``requires_hardened_nozzle`` entry in
       :data:`MATERIAL_PROCESS`, when the material is one we list;
    2. otherwise the fill wording in the name itself.

    The name rule matters because the filled-filament market moves faster
    than any fixed table — ``MATERIAL_PROCESS`` carries PLA-CF but not
    PETG-CF, PA-CF or PLA-GF, and a lookup miss must not read as "not
    abrasive".  Errs toward warning: a needless caution costs a moment,
    a missed one costs a nozzle.
    """
    settings = get_process_settings(material)
    if settings is not None and settings.get("requires_hardened_nozzle"):
        return True

    name = _normalise(material)
    if not name:
        return False
    if any(re.search(p, name) for p in _ABRASIVE_FILL_PATTERNS):
        return True
    for token in re.split(r"[^A-Z0-9]+", name):
        for prefix in _ABRASIVE_FILL_PREFIXES:
            if token.startswith(prefix) and token[len(prefix):].isdigit():
                return True
            if token == prefix:
                return True
    return False


def abrasive_nozzle_floor(material: str) -> str:
    """The always-free nozzle-wear caution for a filled filament.

    Returns ``""`` for an unfilled filament or an empty name — there is
    nothing to warn about, and a caution on every spool is noise that
    trains people to skip the ones that matter.

    Deliberately says nothing about which nozzle is fitted: public Kiln
    holds no nozzle state, so the honest floor is the material fact plus
    what it cannot establish.  Never phrase this as a clearance — a
    filled filament on brass is a real cost, and silence reads as "fine".
    """
    if not is_abrasive_filament(material):
        return ""
    return _ABRASIVE_NOZZLE_FLOOR.format(name=material.strip())


def build_bambu_flush_matrix(materials: list[str], n_slots: int = 4) -> str:
    """Build a BambuStudio-compatible flush volume matrix string.

    BambuStudio's ``project_settings.config`` expects a flattened N×N matrix
    string for the ``[flush_volumes_matrix]`` key.

    Args:
        materials: Material name per slot (index 0 = slot 1).
        n_slots: Total number of slots in the matrix (default 4 for AMS Lite).

    Returns:
        Space-separated string of N*N floats, e.g. ``"0 800 800 800 800 0 ..."``.
    """
    # Pad / truncate to n_slots
    padded = (materials + [""] * n_slots)[:n_slots]
    values: list[str] = []
    for i in range(n_slots):
        for j in range(n_slots):
            if i == j:
                values.append("0")
            elif padded[i] and padded[j]:
                compat = _lookup_compat(padded[i], padded[j])
                vol = compat.get("purge_volume_mm3", 800)
                values.append(str(int(vol)))
            else:
                values.append("800")  # conservative default for unknown slots
    return " ".join(values)
