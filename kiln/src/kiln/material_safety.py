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

import re
from typing import Any

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
