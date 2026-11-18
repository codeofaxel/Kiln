"""Material safety floors: food contact, filled filament, and multi-material prints.

Used by the material doors and the multi-color/multi-material 3MF composer to:

1. **Refuse** a print that pairs materials that must never share one
   (e.g., PLA + ASA), with or without an answer from Kiln's servers.
2. **Check** every pair of loaded materials (level, warning, mitigations,
   purge) and the hardware each needs, through Kiln's servers.
3. **Embed** a BambuStudio-compatible flush volume matrix in the 3MF.
4. **Warn** about food contact and about filled, abrasive filament.

Safety philosophy
-----------------
* Warnings are *always* free tier — users must know about risks.
* Hard blocks are only issued for pairings that are genuinely dangerous
  (extreme temp deltas, reactive chemistries, hardware damage risk).
* Conditional pairings return warnings + recommended mitigations, not blocks.

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


@lru_cache(maxsize=1)
def _catalog_ventilation() -> tuple[frozenset[str], frozenset[str]]:
    """``(catalog keys, keys whose record says ventilation is required)``,
    read straight from the public catalog for the same reason as
    :func:`_food_ratings`.  An unreadable catalog gives two empty sets."""
    try:
        data = json.loads(_materials_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(), frozenset()
    keys = frozenset(k for k, v in data.items() if not k.startswith("_") and isinstance(v, dict))
    vented = frozenset(k for k in keys if (data[k].get("chemical") or {}).get("ventilation_required") is True)
    return keys, vented


def needs_ventilation(material: str) -> bool:
    """True when the public catalog's record for *material* says printing it
    needs ventilation.  The name is matched as the catalog spells it
    (:func:`kiln.catalog_keys.resolve_material_key`, never a family); a
    material the catalog does not hold is ``False``."""
    from kiln.catalog_keys import resolve_material_key

    keys, vented = _catalog_ventilation()
    key = resolve_material_key(material, keys) if isinstance(material, str) else None
    return key in vented


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
# Material names
# ---------------------------------------------------------------------------


def _normalise(material: str) -> str:
    """Normalise material name for lookup (uppercase, strip whitespace)."""
    return material.strip().upper()


# ---------------------------------------------------------------------------
# Multi-material prints: which materials may share one
# ---------------------------------------------------------------------------
#
# Which pairs of materials may share a print, what to purge between them and
# the hardware each needs is Kiln's check (kiln-pro, https://kiln3d.com),
# asked through :mod:`kiln._pro_material_pairing_bridge`.  Every install keeps two
# things so the composer still works with no answer from Kiln's servers: the
# pairs that must never share a print, refused as always, and what the nozzle
# temperature table alone shows about any other pair.

#: Pairs that must never share one print, refused with or without an answer
#: from Kiln's servers.  Each value is the check's own entry for that pair.
_REFUSED_PAIRS: dict[frozenset, dict[str, Any]] = {
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
    frozenset({"ABS", "TPU"}): {
        "level": "incompatible",
        "purge_volume_mm3": 0,
        "warning": "INCOMPATIBLE: ABS (230 °C) and TPU (220 °C) temps overlap but "
                   "ABS requires enclosure / high bed temp that will deform TPU sections.",
        "mitigations": ["Do not pair ABS and TPU."],
    },
}

#: Purge (mm³) between two loaded slots when Kiln's check gave no figure for
#: them: the conservative default, never a judged one.
_CONSERVATIVE_PURGE_MM3 = 800


def nozzle_range(material: str) -> tuple[str, float, float] | None:
    """``(name, nozzle_min, nozzle_max)`` from the nozzle table every install
    carries (:data:`kiln.gcode._MATERIAL_TEMPS`), for a spelling of one of its
    materials (:func:`kiln.catalog_keys.same_material_spelling`); never a
    family, so a filled, blended or brand-named material has no range here."""
    from kiln.catalog_keys import same_material_spelling
    from kiln.gcode import _MATERIAL_TEMPS

    for name, (low, high, _bed_low, _bed_high) in _MATERIAL_TEMPS.items():
        if same_material_spelling(material, name):
            return name, low, high
    return None


def basic_pair_finding(material_a: str, material_b: str) -> tuple[str, bool, str, str]:
    """What the nozzle temperature table alone shows about two materials.

    ``(sentence, temperatures_judged, label_a, label_b)``.  Two spellings of
    one material share its own temperature and bond to themselves.  Ranges
    that do not overlap mean the two can't share one nozzle temperature;
    ranges that overlap say nothing about how well they stick together or how
    much to purge between them; a material the table lacks is said, and
    nothing is judged.  Never says a pair is compatible.
    """
    from kiln.catalog_keys import material_label, same_material_spelling

    range_a = nozzle_range(material_a)
    range_b = nozzle_range(material_b)
    label_a = range_a[0] if range_a else (material_label(material_a) or "an unnamed material")
    label_b = range_b[0] if range_b else (material_label(material_b) or "an unnamed material")
    if same_material_spelling(material_a, material_b):
        return (
            f"Both are {label_a}: one material shares its own nozzle temperature and bonds to itself.",
            True,
            label_a,
            label_a,
        )
    if range_a and range_b:
        span_a = f"{label_a} ({range_a[1]:g}-{range_a[2]:g} °C)"
        span_b = f"{label_b} ({range_b[1]:g}-{range_b[2]:g} °C)"
        if max(range_a[1], range_b[1]) > min(range_a[2], range_b[2]):
            sentence = (
                f"These two can't share one nozzle temperature: the nozzle ranges of "
                f"{span_a} and {span_b} do not overlap. How well they stick together "
                "was not judged."
            )
        else:
            sentence = (
                f"The nozzle ranges of {span_a} and {span_b} overlap. How well they "
                "stick together and how much to purge between them was not judged."
            )
        return sentence, True, label_a, label_b
    unknown = " or ".join(dict.fromkeys(
        label for label, known in ((label_a, range_a), (label_b, range_b)) if not known
    ))
    sentence = (
        f"Kiln's nozzle temperature table has no range for {unknown}, so "
        "neither their temperatures nor how well they stick together was judged."
    )
    return sentence, False, label_a, label_b


def check_material_compatibility(materials: list[str]) -> dict[str, Any]:
    """Check compatibility between all material pairs in a print job.

    Kiln's check answers through :mod:`kiln._pro_material_pairing_bridge`.

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

        When Kiln's check cannot be had, the answer is basic (``basic:
        True``, :func:`_basic_mix_check`): the pairs that must never share a
        print are still refused, every other pair of different materials
        reads ``"caution"`` with the conservative purge and a warning saying
        what the nozzle temperature ranges alone show, the same material in
        two slots reads ``"ok"`` with the conservative purge, and ``caution`` says why the check is
        missing and how to get it, with ``why`` / ``why_code`` /
        ``why_detail`` beside it.
    """
    clean = [_normalise(m) for m in materials if m and m.strip()]
    if not clean:
        return {"safe": True, "level": "ok", "pairs": [], "hardware_warnings": [],
                "purge_matrix": [], "message": "No materials specified."}

    from kiln import _pro_material_pairing_bridge as pairing

    entries, miss = pairing.mix_check(list(dict.fromkeys(clean)))
    if entries is not None:
        check = _check_from_entries(clean, entries)
        if check is not None:
            return check
        from kiln.served_answer import Miss

        miss = Miss("unanswered", detail="the multi-material check left a pair out")
    return _basic_mix_check(clean, miss)


def _check_from_entries(clean: list[str], entries: dict[str, Any]) -> dict[str, Any] | None:
    """The multi-material check for *clean* (one name per loaded part, in
    order), built from Kiln's answer for every pair of its distinct names
    (:func:`kiln._pro_material_pairing_bridge.mix_check`).  A filled filament
    (:func:`is_abrasive_filament`) gets the hardened-nozzle line whatever the
    answer says, as it does with no answer.  ``None`` when the answer leaves
    out a pair the parts need."""
    names = entries["names"]
    by_pair: dict[frozenset, dict[str, Any]] = {
        frozenset({names[i], names[j]}): entry for i, j, entry in entries["pairs"]
    }
    flags = dict(zip(names, entries["hardware"]))

    def entry_for(mat_a: str, mat_b: str) -> dict[str, Any] | None:
        entry = by_pair.get(frozenset({mat_a, mat_b}))
        if entry is None:
            return None
        if entry.get("known"):
            return entry
        # A pair Kiln has no entry for: its own level and purge, worded here.
        return {
            "level": entry["level"],
            "purge_volume_mm3": entry.get("purge_volume_mm3", _CONSERVATIVE_PURGE_MM3),
            "warning": (
                f"Compatibility between {mat_a} and {mat_b} is unknown. "
                "Verify temperature ranges are compatible before printing."
            ),
            "mitigations": [
                f"Check nozzle temp range for both {mat_a} and {mat_b}.",
                "Test with a small print before committing to a full job.",
            ],
        }

    n = len(clean)
    pairs: list[dict[str, Any]] = []
    worst_level = "ok"
    # The check's level is its worst pair's.
    level_rank = {"ok": 0, "caution": 1, "conditional": 2, "incompatible": 3}
    seen: set[frozenset] = set()
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            key = frozenset({clean[i], clean[j]})
            if key in seen:
                continue
            seen.add(key)
            compat = entry_for(clean[i], clean[j])
            if compat is None:
                return None
            pairs.append({
                "material_a": clean[i],
                "material_b": clean[j],
                "level": compat["level"],
                "warning": compat.get("warning"),
                "mitigations": list(compat.get("mitigations", [])),
                "purge_volume_mm3": compat.get("purge_volume_mm3", 80),
            })
            if level_rank.get(compat["level"], 0) > level_rank.get(worst_level, 0):
                worst_level = compat["level"]

    hardware_warnings: list[str] = []
    for mat in clean:
        flag = flags.get(mat) or {}
        if flag.get("hardened_nozzle") or is_abrasive_filament(mat):
            msg = hardened_nozzle_line(mat)
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)
        if flag.get("enclosure"):
            msg = f"{mat} requires an enclosure to prevent warping and fume issues."
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)

    purge_matrix: list[list[float]] = []
    for i in range(n):
        row: list[float] = []
        for j in range(n):
            if i == j:
                row.append(0.0)
            else:
                compat = entry_for(clean[i], clean[j])
                if compat is None:
                    return None
                row.append(float(compat.get("purge_volume_mm3", 80)))
        purge_matrix.append(row)

    incompatible_pairs = [p for p in pairs if p["level"] == "incompatible"]
    if incompatible_pairs:
        message = (
            "⛔ INCOMPATIBLE MATERIALS: "
            + "; ".join(f"{p['material_a']} + {p['material_b']}" for p in incompatible_pairs)
            + ". Do not print — see warnings for details."
        )
    elif worst_level == "conditional":
        message = (
            "⚠️  Conditional compatibility. Review mitigations before printing: "
            + "; ".join(p["warning"] for p in pairs if p.get("warning"))
        )
    elif worst_level == "caution":
        message = "⚠️  Printable with precautions. Review warnings before starting."
    else:
        message = "✅ All material pairings are compatible."

    return {
        "safe": worst_level != "incompatible",
        "level": worst_level,
        "pairs": pairs,
        "hardware_warnings": hardware_warnings,
        "purge_matrix": purge_matrix,
        "message": message,
    }


def _basic_mix_check(clean: list[str], miss: Any) -> dict[str, Any]:
    """The multi-material check this computer can make alone: the refused
    pairs from :data:`_REFUSED_PAIRS`, the conservative purge, the abrasive
    floor, the enclosure line for a material the catalog says needs
    ventilation (:func:`needs_ventilation`), and a warning per other pair
    from :func:`basic_pair_finding`.  The same material in two slots is
    ``"ok"`` with the conservative purge.  With one material loaded there is
    no pair of different materials, and ``caution`` speaks of its hardware
    alone."""
    from kiln.catalog_keys import same_material_spelling
    from kiln.served_answer import Miss, fields, free_account_note, sentence

    if not isinstance(miss, Miss):
        miss = Miss("unanswered", detail="no multi-material check")
    then = "compose again" + free_account_note(miss)
    n = len(clean)
    one_material = all(same_material_spelling(clean[0], m) for m in clean[1:])
    pairs: list[dict[str, Any]] = []
    seen: set[frozenset] = set()
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            key = frozenset({clean[i], clean[j]})
            if key in seen:
                continue
            seen.add(key)
            refused = _REFUSED_PAIRS.get(key)
            if refused is not None:
                entry = {
                    "level": refused["level"],
                    "warning": refused["warning"],
                    "mitigations": list(refused["mitigations"]),
                    "purge_volume_mm3": refused["purge_volume_mm3"],
                }
            elif same_material_spelling(clean[i], clean[j]):
                # One material in two slots: it shares its own temperature and
                # bonds to itself; only the purge between colours is unjudged.
                entry = {
                    "level": "ok",
                    "warning": basic_pair_finding(clean[i], clean[j])[0]
                    + " The purge between its colours was not judged.",
                    "mitigations": [],
                    "purge_volume_mm3": _CONSERVATIVE_PURGE_MM3,
                }
            else:
                entry = {
                    "level": "caution",
                    "warning": basic_pair_finding(clean[i], clean[j])[0],
                    "mitigations": [],
                    "purge_volume_mm3": _CONSERVATIVE_PURGE_MM3,
                }
            pairs.append({"material_a": clean[i], "material_b": clean[j], **entry})

    hardware_warnings: list[str] = []
    for mat in clean:
        if is_abrasive_filament(mat):
            msg = hardened_nozzle_line(mat)
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)
        if needs_ventilation(mat):
            msg = f"{mat} requires an enclosure to prevent warping and fume issues."
            if msg not in hardware_warnings:
                hardware_warnings.append(msg)

    purge_matrix: list[list[float]] = []
    for i in range(n):
        row: list[float] = []
        for j in range(n):
            refused = _REFUSED_PAIRS.get(frozenset({clean[i], clean[j]}))
            if i == j:
                row.append(0.0)
            elif refused is not None:
                row.append(float(refused["purge_volume_mm3"]))
            else:
                row.append(float(_CONSERVATIVE_PURGE_MM3))
        purge_matrix.append(row)

    incompatible_pairs = [p for p in pairs if p["level"] == "incompatible"]
    if incompatible_pairs:
        level = "incompatible"
        message = (
            "⛔ INCOMPATIBLE MATERIALS: "
            + "; ".join(f"{p['material_a']} + {p['material_b']}" for p in incompatible_pairs)
            + ". Do not print — see warnings for details."
        )
    elif any(p["level"] != "ok" for p in pairs):
        level = "caution"
        message = "⚠️  Printable with precautions. Review warnings before starting."
    elif pairs:
        level = "ok"
        message = "One material in every slot, so there are no pairs of different materials to check."
    else:
        level = "ok"
        message = "One material, so there are no pairs to check."
    if one_material:
        # One material: no pair to judge, only the hardware it needs.
        caution = sentence(
            miss,
            feature="material check",
            on_the_line="Kiln's material check says what hardware a loaded material needs",
            cannot="check what hardware this material needs",
            wont=(
                "warned only from the material's name and the catalog on this computer"
                + (", and set the conservative purge between its colours" if n > 1 else "")
            ),
            then=then,
        )
    else:
        caution = sentence(
            miss,
            feature="multi-material check",
            on_the_line=(
                "Kiln's multi-material check says which loaded materials work together "
                "and how much to purge between them"
            ),
            cannot="check how these materials pair",
            wont=(
                "refused only the pairs that must never share a print and set the "
                "conservative purge between the others"
            ),
            safe_remedy="run a small test piece before a long print",
            then=then,
        )
    return {
        "safe": not incompatible_pairs,
        "level": level,
        "pairs": pairs,
        "hardware_warnings": hardware_warnings,
        "purge_matrix": purge_matrix,
        "message": message,
        "basic": True,
        "caution": caution,
        **fields(miss),
    }


# ---------------------------------------------------------------------------
# Abrasive-filament floor
# ---------------------------------------------------------------------------

#: The fillers a filled filament is named for, as they appear before "fill".
_FILLERS = (
    r"COPPER|BRONZE|BRASS|STEEL|IRON|ALUMINI?UM|TITANIUM|TUNGSTEN|METAL|MAGNET(?:IC)?"
    r"|WOOD|TIMBER|BAMBOO|HEMP|CORK|STONE|MARBLE|MINERAL|CERAMIC|GLASS|CARBON|GLOW"
)

#: Fill wording in a filled filament's own name.  Whole words, because
#: "CARBON" as a bare substring reads POLYCARBONATE as carbon-filled, and
#: "GLASS" alone reads a sea-glass colourway as glass fibre; "carbon black"
#: is a pigment, not a filler.  Metal fill is matched with its "fill":
#: "Copper" on a spool is far more often a colour than a filler, but
#: "Copperfill" never is.  "Fill" on its own is a product word ("Refill",
#: "Extrafill", a maker's name), so it counts only after a filler or as
#: "filled".
_ABRASIVE_FILL_PATTERNS = (
    r"\bCARBON\b(?!\s*-?\s*BLACK)",
    r"\bWOOD\b",
    r"\bMETAL\b",
    r"\bMAGNETIC\b",
    r"\bGLOW\b",
    r"GLASS\s*-?\s*FIB",
    rf"(?:{_FILLERS})\s*-?\s*FILL",
    r"\bFILLED\b",
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

    Read from the fill wording in the name itself, with no table: the
    filled-filament market moves faster than any fixed list, and a lookup
    miss must not read as "not abrasive".  Errs toward warning: a needless
    caution costs a moment, a missed one costs a nozzle.
    """
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


def hardened_nozzle_line(material: str) -> str:
    """The hardware line a multi-material answer gives a material that needs a
    hardened nozzle, naming it as written."""
    return f"{material} requires a hardened/wear-resistant nozzle (not standard brass)."


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


def build_bambu_flush_matrix(
    materials: list[str],
    n_slots: int = 4,
    *,
    check: dict[str, Any] | None = None,
) -> str:
    """Build a BambuStudio-compatible flush volume matrix string.

    BambuStudio's ``project_settings.config`` expects a flattened N×N matrix
    string for the ``[flush_volumes_matrix]`` key.

    Args:
        materials: Material name per slot (index 0 = slot 1).
        n_slots: Total number of slots in the matrix (default 4 for AMS Lite).
        check: :func:`check_material_compatibility`'s answer for the print's
            materials when the caller already holds it; asked for otherwise.
            Each pair's purge is read from it.  A pair it gives no figure for,
            and a slot with no material, gets the conservative purge.

    Returns:
        Space-separated string of N*N floats, e.g. ``"0 800 800 800 800 0 ..."``.
    """
    # Pad / truncate to n_slots
    padded = (materials + [""] * n_slots)[:n_slots]
    if check is None:
        named = [m for m in padded if m and m.strip()]
        check = check_material_compatibility(named) if named else {"pairs": []}
    purge: dict[frozenset, Any] = {}
    for pair in check.get("pairs") or []:
        if isinstance(pair, dict):
            key = frozenset({_normalise(str(pair.get("material_a") or "")), _normalise(str(pair.get("material_b") or ""))})
            purge[key] = pair.get("purge_volume_mm3")
    values: list[str] = []
    for i in range(n_slots):
        for j in range(n_slots):
            if i == j:
                values.append("0")
            elif padded[i] and padded[j]:
                vol = purge.get(frozenset({_normalise(padded[i]), _normalise(padded[j])}))
                ok = isinstance(vol, (int, float)) and not isinstance(vol, bool)
                values.append(str(int(vol)) if ok else str(_CONSERVATIVE_PURGE_MM3))
            else:
                values.append(str(_CONSERVATIVE_PURGE_MM3))  # conservative default for unknown slots
    return " ".join(values)
