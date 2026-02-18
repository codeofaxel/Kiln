"""Printer intelligence database — firmware quirks, material compatibility,
calibration guidance, and known failure modes.

Ships a curated JSON database of operational knowledge for popular 3D
printers.  Agents query this to make informed decisions without
trial-and-error.

Usage::

    from kiln.printer_intelligence import get_printer_intel, list_intel_profiles

    intel = get_printer_intel("ender3")
    print(intel.materials["PLA"])       # {"hotend": 200, "bed": 60, ...}
    print(intel.quirks)                 # ["PTFE tube degrades above 240C...", ...]
    print(intel.failure_modes[0])       # {"symptom": ..., "cause": ..., "fix": ...}
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
    profile = _cache.get(normalised)
    if profile is not None:
        return profile

    for key in _cache:
        if normalised.startswith(key) or key.startswith(normalised):
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
        "materials": {
            name: {"hotend": mp.hotend, "bed": mp.bed, "fan": mp.fan, "notes": mp.notes}
            for name, mp in intel.materials.items()
        },
        "quirks": intel.quirks,
        "calibration": intel.calibration,
        "failure_modes": [{"symptom": fm.symptom, "cause": fm.cause, "fix": fm.fix} for fm in intel.failure_modes],
    }
