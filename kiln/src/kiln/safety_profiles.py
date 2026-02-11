"""Bundled safety profiles for printer-specific G-code validation.

Ships a curated JSON database of per-printer safety limits (temperatures,
feedrates, volumetric flow, build volume) so that the G-code validator
can enforce tighter — or more permissive — constraints when the target
printer is known.

Usage::

    from kiln.safety_profiles import get_profile, list_profiles

    profile = get_profile("ender3")
    print(profile.max_hotend_temp)   # 260.0
    print(profile.notes)             # "PTFE-lined hotend ..."

    all_ids = list_profiles()        # ["default", "ender3", ...]
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "safety_profiles.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafetyProfile:
    """Validated safety limits for a specific printer model.

    Attributes:
        id: Short identifier key (e.g. ``"ender3"``, ``"bambu_x1c"``).
        display_name: Human-readable name.
        max_hotend_temp: Absolute max hotend temperature (°C).
        max_bed_temp: Absolute max bed temperature (°C).
        max_chamber_temp: Max chamber temperature (°C), or ``None``.
        max_feedrate: Max recommended feedrate (mm/min).
        min_safe_z: Minimum safe Z value (mm).  Usually 0.
        max_volumetric_flow: Max volumetric flow (mm³/s), or ``None``.
        build_volume: ``[X, Y, Z]`` build dimensions in mm, or ``None``.
        notes: Free-text notes about the printer's safety characteristics.
    """

    id: str
    display_name: str
    max_hotend_temp: float
    max_bed_temp: float
    max_chamber_temp: Optional[float]
    max_feedrate: float
    min_safe_z: float
    max_volumetric_flow: Optional[float]
    build_volume: Optional[List[int]]
    notes: str


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: Dict[str, SafetyProfile] = {}
_loaded: bool = False


def _load() -> None:
    """Load profiles from the bundled JSON file.  Called once on first access."""
    global _loaded
    if _loaded:
        return

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load safety profiles: %s", exc)
        _loaded = True
        return

    for key, data in raw.items():
        if key == "_meta":
            continue
        try:
            _cache[key] = SafetyProfile(
                id=key,
                display_name=data.get("display_name", key),
                max_hotend_temp=float(data["max_hotend_temp"]),
                max_bed_temp=float(data["max_bed_temp"]),
                max_chamber_temp=float(data["max_chamber_temp"])
                if data.get("max_chamber_temp") is not None
                else None,
                max_feedrate=float(data.get("max_feedrate", 10_000)),
                min_safe_z=float(data.get("min_safe_z", 0.0)),
                max_volumetric_flow=float(data["max_volumetric_flow"])
                if data.get("max_volumetric_flow") is not None
                else None,
                build_volume=data.get("build_volume"),
                notes=data.get("notes", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed safety profile '%s': %s", key, exc)

    _loaded = True
    logger.debug("Loaded %d safety profiles from %s", len(_cache), _DATA_FILE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_profile(printer_id: str) -> SafetyProfile:
    """Return the safety profile for *printer_id*.

    Falls back to the ``"default"`` profile if no specific profile is
    found.  Raises ``KeyError`` only if even the default is missing
    (shouldn't happen with the bundled data).

    Args:
        printer_id: Short identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
            Case-insensitive; hyphens are normalised to underscores.
    """
    _load()
    normalised = printer_id.lower().replace("-", "_").strip()
    profile = _cache.get(normalised)
    if profile is not None:
        return profile

    # Try fuzzy prefix match (e.g. "ender-3-v2" → "ender3").
    for key in _cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _cache[key]

    default = _cache.get("default")
    if default is not None:
        return default

    raise KeyError(f"No safety profile for '{printer_id}' and no default profile available.")


def list_profiles() -> List[str]:
    """Return all available profile IDs sorted alphabetically."""
    _load()
    return sorted(_cache.keys())


def get_all_profiles() -> Dict[str, SafetyProfile]:
    """Return all loaded profiles as a dict keyed by profile ID."""
    _load()
    return dict(_cache)


def profile_to_dict(profile: SafetyProfile) -> Dict[str, Any]:
    """Serialise a :class:`SafetyProfile` to a plain dict for MCP responses."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "max_hotend_temp": profile.max_hotend_temp,
        "max_bed_temp": profile.max_bed_temp,
        "max_chamber_temp": profile.max_chamber_temp,
        "max_feedrate": profile.max_feedrate,
        "min_safe_z": profile.min_safe_z,
        "max_volumetric_flow": profile.max_volumetric_flow,
        "build_volume": profile.build_volume,
        "notes": profile.notes,
    }
