"""Printer intelligence database — firmware quirks, material compatibility,
calibration guidance, speed intelligence, and known failure modes.

Ships a curated JSON database of operational knowledge for popular 3D
printers.  Agents query this to make informed decisions without
trial-and-error.

Usage::

    from kiln.printer_intelligence import get_printer_intel, list_intel_profiles

    intel = get_printer_intel("ender3")
    print(intel.materials["PLA"])       # {"hotend": 200, "bed": 60, ...}
    print(intel.quirks)                 # ["PTFE tube degrades above 240C...", ...]
    print(intel.failure_modes[0])       # {"symptom": ..., "cause": ..., "fix": ...}

Speed intelligence::

    from kiln.printer_intelligence import get_slicer_speed_overrides

    overrides = get_slicer_speed_overrides("bambu_a1")
    # Returns PrusaSlicer INI keys tuned for the Bambu A1's capabilities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "printer_intelligence.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialProfile:
    """Recommended settings for a specific material on a specific printer."""

    hotend: int
    bed: int
    fan: int
    notes: str = ""


@dataclass(frozen=True)
class FailureMode:
    """Known failure pattern and resolution."""

    symptom: str
    cause: str
    fix: str


@dataclass(frozen=True)
class PrinterIntel:
    """Operational intelligence for a specific printer model.

    Attributes:
        id: Short identifier matching safety_profiles.json.
        display_name: Human-readable name.
        firmware: Firmware type (``"marlin"``, ``"klipper"``, ``"bambu"``).
        extruder_type: ``"direct_drive"`` or ``"bowden"``.
        hotend_type: ``"all_metal"`` or ``"ptfe_lined"``.
        has_enclosure: Whether the printer has a stock enclosure.
        has_abl: Whether automatic bed leveling is available.
        capabilities: Extended model facts such as camera and multicolor support.
        materials: Material compatibility map (name → settings).
        quirks: List of printer-specific gotchas and tips.
        calibration: Calibration guidance keyed by procedure name.
        failure_modes: Known failure patterns with fixes.
    """

    id: str
    display_name: str
    firmware: str
    extruder_type: str
    hotend_type: str
    has_enclosure: bool
    has_abl: bool
    capabilities: dict[str, Any]
    materials: dict[str, MaterialProfile]
    quirks: list[str]
    calibration: dict[str, str]
    failure_modes: list[FailureMode]


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: dict[str, PrinterIntel] = {}
_loaded: bool = False


def _load() -> None:
    global _loaded
    if _loaded:
        return

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load printer intelligence: %s", exc)
        _loaded = True
        return

    for key, data in raw.items():
        if key == "_meta":
            continue
        try:
            materials = {}
            for mat_name, mat_data in data.get("materials", {}).items():
                materials[mat_name] = MaterialProfile(
                    hotend=int(mat_data["hotend"]),
                    bed=int(mat_data["bed"]),
                    fan=int(mat_data["fan"]),
                    notes=mat_data.get("notes", ""),
                )

            failure_modes = []
            for fm in data.get("failure_modes", []):
                failure_modes.append(
                    FailureMode(
                        symptom=fm["symptom"],
                        cause=fm["cause"],
                        fix=fm["fix"],
                    )
                )

            _cache[key] = PrinterIntel(
                id=key,
                display_name=data.get("display_name", key),
                firmware=data.get("firmware", "marlin"),
                extruder_type=data.get("extruder_type", "direct_drive"),
                hotend_type=data.get("hotend_type", "all_metal"),
                has_enclosure=bool(data.get("has_enclosure", False)),
                has_abl=bool(data.get("has_abl", False)),
                capabilities=dict(data.get("capabilities", {})),
                materials=materials,
                quirks=list(data.get("quirks", [])),
                calibration=dict(data.get("calibration", {})),
                failure_modes=failure_modes,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed intel profile '%s': %s", key, exc)

    _loaded = True
    logger.debug("Loaded %d printer intel profiles from %s", len(_cache), _DATA_FILE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_printer_intel(printer_id: str) -> PrinterIntel:
    """Return operational intelligence for *printer_id*.

    Falls back to the ``"default"`` profile if no match is found.
    """
    _load()
    normalised = printer_id.lower().replace("-", "_").strip()
    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.append(normalised.removeprefix("creality_"))
    for candidate in candidates:
        profile = _cache.get(candidate)
        if profile is not None:
            return profile

    for key in _cache:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
                return _cache[key]

    default = _cache.get("default")
    if default is not None:
        return default
    raise KeyError(f"No printer intelligence for '{printer_id}' and no default available.")


def list_intel_profiles() -> list[str]:
    """Return all available printer intel profile IDs."""
    _load()
    return sorted(_cache.keys())


def get_material_settings(
    printer_id: str,
    material: str,
) -> MaterialProfile | None:
    """Get recommended settings for a material on a specific printer.

    Returns ``None`` if the material isn't in the printer's profile.
    """
    intel = get_printer_intel(printer_id)
    return intel.materials.get(material.upper())


def diagnose_issue(
    printer_id: str,
    symptom: str,
) -> list[dict[str, str]]:
    """Search failure modes for matching symptoms.

    Returns a list of matching ``{symptom, cause, fix}`` dicts.
    """
    intel = get_printer_intel(printer_id)
    symptom_lower = symptom.lower()
    matches = []
    for fm in intel.failure_modes:
        if (
            symptom_lower in fm.symptom.lower()
            or symptom_lower in fm.cause.lower()
            or any(word in fm.symptom.lower() for word in symptom_lower.split() if len(word) > 3)
        ):
            matches.append(
                {
                    "symptom": fm.symptom,
                    "cause": fm.cause,
                    "fix": fm.fix,
                }
            )
    return matches


def intel_to_dict(intel: PrinterIntel) -> dict[str, Any]:
    """Serialise a :class:`PrinterIntel` to a plain dict for MCP responses."""
    return {
        "id": intel.id,
        "display_name": intel.display_name,
        "firmware": intel.firmware,
        "extruder_type": intel.extruder_type,
        "hotend_type": intel.hotend_type,
        "has_enclosure": intel.has_enclosure,
        "has_abl": intel.has_abl,
        "capabilities": intel.capabilities,
        "materials": {
            name: {"hotend": mp.hotend, "bed": mp.bed, "fan": mp.fan, "notes": mp.notes}
            for name, mp in intel.materials.items()
        },
        "quirks": intel.quirks,
        "calibration": intel.calibration,
        "failure_modes": [{"symptom": fm.symptom, "cause": fm.cause, "fix": fm.fix} for fm in intel.failure_modes],
    }


# ---------------------------------------------------------------------------
# Raw JSON cache (for fields not captured by the PrinterIntel dataclass)
# ---------------------------------------------------------------------------

_raw_cache: dict[str, dict[str, Any]] = {}
_raw_loaded: bool = False


def _load_raw() -> None:
    """Load the raw JSON dict so we can read extended fields like speed data."""
    global _raw_loaded
    if _raw_loaded:
        return
    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load raw printer intelligence: %s", exc)
        _raw_loaded = True
        return
    for key, data in raw.items():
        if key == "_meta":
            continue
        _raw_cache[key] = data
    _raw_loaded = True


def _get_raw(printer_id: str) -> dict[str, Any] | None:
    """Return the raw JSON entry for *printer_id*, or ``None``."""
    _load_raw()
    normalised = printer_id.lower().replace("-", "_").strip()
    entry = _raw_cache.get(normalised)
    if entry is not None:
        return entry
    # Fuzzy prefix match (same logic as get_printer_intel).
    for key in _raw_cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _raw_cache[key]
    return None


# ---------------------------------------------------------------------------
# Per-printer speed intelligence for PrusaSlicer
# ---------------------------------------------------------------------------

# Curated speed capability table keyed by printer_id.
# Values: (max_print_speed_mm_s, max_accel_mm_s2, has_input_shaping, quality_factor)
# quality_factor: fraction of max speed to use for quality prints (0.0-1.0).
# slicer_time_factor: multiplier to correct PrusaSlicer's time estimate for
#   this printer.  PrusaSlicer doesn't model input shaping or high acceleration,
#   so it overestimates by ~2x for modern Bambu/Creality K1 printers.
#   Applied to M73 R (remaining time) commands before upload so the printer
#   LCD shows accurate time from the first second.  1.0 = no correction.
_SPEED_CAPABILITIES: dict[str, dict[str, Any]] = {
    # --- Bambu Lab ---
    "bambu_a1": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    "bambu_a1_mini": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.70,
        "slicer_time_factor": 0.50,
    },
    "bambu_x1c": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.80,
        "slicer_time_factor": 0.45,
    },
    "bambu_p1s": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.78,
        "slicer_time_factor": 0.48,
    },
    "bambu_p1p": {
        "max_speed": 300,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    # --- Creality ---
    "ender3": {
        "max_speed": 60,
        "max_accel": 500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "ender3_v2": {
        "max_speed": 70,
        "max_accel": 600,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "ender3_s1": {
        "max_speed": 100,
        "max_accel": 1500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "k1": {
        "max_speed": 300,
        "max_accel": 12000,
        "input_shaping": True,
        "quality_factor": 0.75,
        "slicer_time_factor": 0.50,
    },
    # --- Prusa ---
    "prusa_mk3s": {
        "max_speed": 100,
        "max_accel": 1250,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "prusa_mk4": {
        "max_speed": 150,
        "max_accel": 4000,
        "input_shaping": True,
        "quality_factor": 0.78,
    },
    "prusa_mini": {
        "max_speed": 100,
        "max_accel": 1000,
        "input_shaping": False,
        "quality_factor": 0.70,
    },
    "prusa_xl": {
        "max_speed": 150,
        "max_accel": 4000,
        "input_shaping": True,
        "quality_factor": 0.78,
    },
    # --- Voron ---
    "voron_2": {
        "max_speed": 300,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    "voron_0": {
        "max_speed": 250,
        "max_accel": 8000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    # --- Klipper generic ---
    "klipper_generic": {
        "max_speed": 150,
        "max_accel": 3000,
        "input_shaping": True,
        "quality_factor": 0.70,
    },
    # --- Elegoo ---
    "elegoo_neptune3": {
        "max_speed": 60,
        "max_accel": 500,
        "input_shaping": False,
        "quality_factor": 0.75,
    },
    "elegoo_neptune4": {
        "max_speed": 250,
        "max_accel": 8000,
        "input_shaping": True,
        "quality_factor": 0.72,
    },
}

# Generic type-level fallbacks when no printer_id matches.
_TYPE_SPEED_DEFAULTS: dict[str, dict[str, Any]] = {
    "bambu": {
        "max_speed": 250,
        "max_accel": 10000,
        "input_shaping": True,
        "quality_factor": 0.75,
    },
    "octoprint": {
        "max_speed": 80,
        "max_accel": 800,
        "input_shaping": False,
        "quality_factor": 0.70,
    },
    "moonraker": {
        "max_speed": 150,
        "max_accel": 3000,
        "input_shaping": True,
        "quality_factor": 0.70,
    },
}


def _build_speed_overrides(caps: dict[str, Any]) -> dict[str, str]:
    """Convert a speed capability dict into PrusaSlicer INI key-value pairs.

    Applies the quality_factor safety margin so prints use a fraction of the
    printer's advertised maximum, yielding better surface quality while still
    being significantly faster than PrusaSlicer's conservative defaults.
    """
    max_speed: int = caps["max_speed"]
    max_accel: int = caps["max_accel"]
    has_is: bool = caps["input_shaping"]
    qf: float = caps["quality_factor"]

    # Derive operational speeds from max capability * quality factor.
    perimeter = int(max_speed * qf)
    # External perimeters need to be slower for surface quality.
    external_perimeter = int(perimeter * 0.65)
    infill = int(max_speed * qf * 1.05)  # infill can be slightly faster
    infill = min(infill, max_speed)  # but never exceed hardware max
    solid_infill = int(perimeter * 0.90)
    top_solid_infill = int(external_perimeter * 0.90)
    first_layer = max(15, int(max_speed * 0.20))  # 20% of max, floor 15
    first_layer = min(first_layer, 40)  # cap at 40mm/s for reliability
    # Travel can be close to max — no extrusion quality concerns.
    travel = int(max_speed * 0.95)
    travel = min(travel, 300)  # PrusaSlicer cap is typically 300
    max_print = int(max_speed * qf)

    overrides: dict[str, str] = {
        "perimeter_speed": str(perimeter),
        "external_perimeter_speed": str(external_perimeter),
        "infill_speed": str(infill),
        "solid_infill_speed": str(solid_infill),
        "top_solid_infill_speed": str(top_solid_infill),
        "first_layer_speed": str(first_layer),
        "travel_speed": str(travel),
        "max_print_speed": str(max_print),
    }

    # Acceleration overrides — only if the printer supports meaningful accel.
    # PrusaSlicer's default_acceleration=0 means "firmware default", but when
    # we know the printer's capability we can set it explicitly.
    if max_accel >= 500:
        # Use ~70% of max accel for general printing.
        default_accel = int(max_accel * 0.70)
        # First layer uses much lower acceleration for bed adhesion.
        first_layer_accel = max(200, int(max_accel * 0.20))
        first_layer_accel = min(first_layer_accel, 1000)
        overrides["default_acceleration"] = str(default_accel)
        overrides["first_layer_acceleration"] = str(first_layer_accel)

    # Input-shaping-aware printers tolerate higher accelerations on
    # perimeters without ringing, so we don't need to derate as much.
    if has_is and max_accel >= 3000:
        perimeter_accel = int(max_accel * 0.55)
        overrides["perimeter_acceleration"] = str(perimeter_accel)
        overrides["infill_acceleration"] = str(int(max_accel * 0.80))
        # External perimeter acceleration slightly lower for surface finish.
        overrides["external_perimeter_acceleration"] = str(int(max_accel * 0.45))

    return overrides


def _resolve_caps(printer_id: str) -> dict[str, Any] | None:
    """Resolve speed capabilities for a printer.

    Priority order:
    1. Curated ``_SPEED_CAPABILITIES`` table (hand-tuned practical limits).
    2. Raw JSON extended fields (``max_speed_mm_s``) — these represent
       hardware maximums and get derated via a conservative quality_factor.
    3. Fuzzy prefix match on either source.
    """
    normalised = printer_id.lower().replace("-", "_").strip()

    # 1. Curated table — exact match (preferred: hand-tuned practical limits).
    caps = _SPEED_CAPABILITIES.get(normalised)
    if caps is not None:
        return caps

    # 2. Fuzzy prefix match on curated table.
    for key in _SPEED_CAPABILITIES:
        if normalised.startswith(key) or key.startswith(normalised):
            return _SPEED_CAPABILITIES[key]

    # 3. Fall back to raw JSON extended fields.  These are hardware maximums
    #    (e.g. 500 mm/s for bambu_a1) so we use a lower quality_factor to
    #    derate to practical printing speeds.
    raw = _get_raw(normalised)
    if raw and "max_speed_mm_s" in raw:
        return {
            "max_speed": int(raw["max_speed_mm_s"]),
            "max_accel": int(raw.get("max_acceleration_mm_s2", 5000)),
            "input_shaping": "input shaping" in " ".join(raw.get("quirks", [])).lower()
            or raw.get("firmware") == "bambu"
            or raw.get("firmware") == "klipper",
            "quality_factor": 0.50,  # conservative: hardware max != practical max
        }

    return None


def get_slicer_speed_overrides(printer_id: str) -> dict[str, str]:
    """Generate PrusaSlicer speed overrides tuned for a specific printer model.

    Uses the printer intelligence database to produce optimal speed,
    acceleration, and jerk settings for PrusaSlicer.  This ensures that
    prints sliced via Kiln run at the printer's actual capability
    instead of PrusaSlicer's conservative defaults.

    The returned dict maps PrusaSlicer INI keys to string values,
    ready for injection into the slicer command line or profile.

    Args:
        printer_id: Printer model identifier (e.g. ``"bambu_a1"``,
            ``"ender3"``, ``"voron_2.4"``, ``"prusa_mk4"``,
            ``"bambu_x1c"``, ``"klipper_generic"``).

    Returns:
        Dict of PrusaSlicer speed overrides.  Empty dict if the printer
        is not recognized (falls back to PrusaSlicer defaults).
    """
    caps = _resolve_caps(printer_id)
    if caps is None:
        return {}
    return _build_speed_overrides(caps)


def get_slicer_time_factor(printer_id: str) -> float:
    """Return the slicer time correction factor for a printer.

    PrusaSlicer overestimates print time for printers with input shaping
    (Bambu, Creality K1) because it doesn't model their acceleration
    profiles.  This factor corrects the estimate: multiply PrusaSlicer's
    time by this value to get the real expected print time.

    Returns 1.0 (no correction) for unknown printers.
    """
    caps = _resolve_caps(printer_id)
    if caps is None:
        return 1.0
    return caps.get("slicer_time_factor", 1.0)


def get_slicer_speed_overrides_for_type(printer_type: str) -> dict[str, str]:
    """Fallback: get generic speed overrides by printer type.

    Useful when the exact printer model is unknown but the connection
    type is known (e.g. ``"bambu"``, ``"octoprint"``, ``"moonraker"``).

    Args:
        printer_type: One of ``"bambu"``, ``"octoprint"``, or
            ``"moonraker"``.

    Returns:
        Dict of PrusaSlicer speed overrides for a generic printer
        of the given type.  Empty dict if the type is not recognized.
    """
    normalised = printer_type.lower().strip()
    caps = _TYPE_SPEED_DEFAULTS.get(normalised)
    if caps is None:
        return {}
    return _build_speed_overrides(caps)
