"""Bundled and community safety profiles for printer-specific G-code validation.

Ships a curated JSON database of per-printer safety limits (temperatures,
feedrates, volumetric flow, build volume) so that the G-code validator
can enforce tighter — or more permissive — constraints when the target
printer is known.

Community profiles are stored in ``~/.kiln/community_profiles.json`` and
take precedence over bundled profiles, allowing users to contribute and
override safety limits for printers not in the bundled database.

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
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "safety_profiles.json"
_COMMUNITY_DIR = Path.home() / ".kiln"
_COMMUNITY_FILE = _COMMUNITY_DIR / "community_profiles.json"

# Validation constants
_MAX_TEMP_CEILING = 500.0
_MAX_FEEDRATE_CEILING = 2000.0  # mm/s (not mm/min — this is the user-facing limit)
_REQUIRED_FIELDS = ("max_hotend_temp", "max_bed_temp", "max_feedrate", "build_volume")


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
    max_chamber_temp: float | None
    max_feedrate: float
    min_safe_z: float
    max_volumetric_flow: float | None
    build_volume: list[int] | None
    notes: str


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: dict[str, SafetyProfile] = {}
_community_cache: dict[str, SafetyProfile] = {}
_loaded: bool = False
_community_loaded: bool = False


def _parse_profiles(raw: dict[str, Any], target: dict[str, SafetyProfile]) -> None:
    """Parse raw JSON profile entries into *target* dict."""
    for key, data in raw.items():
        if key == "_meta":
            continue
        try:
            target[key] = SafetyProfile(
                id=key,
                display_name=data.get("display_name", key),
                max_hotend_temp=float(data["max_hotend_temp"]),
                max_bed_temp=float(data["max_bed_temp"]),
                max_chamber_temp=float(data["max_chamber_temp"]) if data.get("max_chamber_temp") is not None else None,
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

    _parse_profiles(raw, _cache)
    _loaded = True
    logger.debug("Loaded %d safety profiles from %s", len(_cache), _DATA_FILE)


def _load_community() -> None:
    """Load community profiles from ``~/.kiln/community_profiles.json``."""
    global _community_loaded
    if _community_loaded:
        return

    if not _COMMUNITY_FILE.exists():
        _community_loaded = True
        return

    try:
        raw = json.loads(_COMMUNITY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load community profiles: %s", exc)
        _community_loaded = True
        return

    _parse_profiles(raw, _community_cache)
    _community_loaded = True
    logger.debug(
        "Loaded %d community profiles from %s",
        len(_community_cache),
        _COMMUNITY_FILE,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_profile(printer_id: str) -> SafetyProfile:
    """Return the safety profile for *printer_id*.

    Community profiles (``~/.kiln/community_profiles.json``) take
    precedence over bundled profiles.  Falls back to the ``"default"``
    profile if no specific profile is found.  Raises ``KeyError`` only
    if even the default is missing (shouldn't happen with the bundled
    data).

    Args:
        printer_id: Short identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
            Case-insensitive; hyphens are normalised to underscores.
    """
    _load()
    _load_community()
    normalised = printer_id.lower().replace("-", "_").strip()

    # Community profiles take precedence over bundled.
    community = _community_cache.get(normalised)
    if community is not None:
        return community

    profile = _cache.get(normalised)
    if profile is not None:
        return profile

    # Try fuzzy prefix match (e.g. "ender-3-v2" → "ender3").
    for key in _community_cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _community_cache[key]
    for key in _cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _cache[key]

    default = _cache.get("default")
    if default is not None:
        return default

    raise KeyError(f"No safety profile for '{printer_id}' and no default profile available.")


def list_profiles() -> list[str]:
    """Return all available profile IDs sorted alphabetically.

    Includes both bundled and community profiles.
    """
    _load()
    _load_community()
    return sorted(set(_cache.keys()) | set(_community_cache.keys()))


def get_all_profiles() -> dict[str, SafetyProfile]:
    """Return all loaded profiles as a dict keyed by profile ID."""
    _load()
    return dict(_cache)


def match_display_name(name: str) -> str | None:
    """Fuzzy-match a human-readable printer name to a profile ID.

    Tries normalised matching (lowercase, strip separators) and substring
    matching against all loaded profiles' ``display_name`` fields.

    Returns the profile ID if found, or ``None``.
    """
    _load()
    normalised = name.lower().replace("-", "_").replace(" ", "_").strip("_")

    # Check display_name fields
    for key, profile in _cache.items():
        if key == "default":
            continue
        dn = profile.display_name.lower().replace("-", "_").replace(" ", "_").strip("_")
        if normalised == dn or normalised in dn or dn in normalised:
            return key

    # Fallback: try key matching
    for key in _cache:
        if key == "default":
            continue
        if normalised.startswith(key) or key.startswith(normalised):
            return key

    return None


def resolve_limits(printer_id: str | None = None) -> tuple:
    """Return ``(max_hotend, max_bed)`` for a printer, with fallback.

    When *printer_id* is provided, loads the matching profile.  Falls back
    to the default profile, and finally to conservative generic limits
    (300/130) if no profile data is available at all.
    """
    if printer_id:
        try:
            profile = get_profile(printer_id)
            return profile.max_hotend_temp, profile.max_bed_temp
        except KeyError:
            pass
    # Try the default profile
    try:
        default = get_profile("default")
        return default.max_hotend_temp, default.max_bed_temp
    except KeyError:
        return 300.0, 130.0


def profile_to_dict(profile: SafetyProfile) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Community profile contribution
# ---------------------------------------------------------------------------


def validate_safety_profile(profile: dict[str, Any]) -> list[str]:
    """Validate a candidate safety profile dict.

    Returns a list of human-readable error strings.  An empty list means
    the profile is valid and safe to persist.

    Checks:
    - All required fields are present (``max_hotend_temp``,
      ``max_bed_temp``, ``max_feedrate``, ``build_volume``).
    - Temperature values are numeric and within ``[0, 500]``.
    - Feedrate is numeric and within ``[0, 2000]`` mm/s.
    - Build volume is a list of 3 positive numbers.
    """
    errors: list[str] = []

    # --- required fields ---
    for field in _REQUIRED_FIELDS:
        if field not in profile:
            errors.append(f"Missing required field: {field}")

    # Early-out if required fields are absent — further checks would KeyError.
    if errors:
        return errors

    # --- type + range: temperatures ---
    for temp_field in ("max_hotend_temp", "max_bed_temp"):
        val = profile[temp_field]
        if not isinstance(val, (int, float)):
            errors.append(f"{temp_field} must be a number, got {type(val).__name__}")
        elif not (0 <= val <= _MAX_TEMP_CEILING):
            errors.append(f"{temp_field} must be between 0 and {_MAX_TEMP_CEILING}, got {val}")

    # Optional chamber temp — same range when present.
    if "max_chamber_temp" in profile and profile["max_chamber_temp"] is not None:
        val = profile["max_chamber_temp"]
        if not isinstance(val, (int, float)):
            errors.append(f"max_chamber_temp must be a number, got {type(val).__name__}")
        elif not (0 <= val <= _MAX_TEMP_CEILING):
            errors.append(f"max_chamber_temp must be between 0 and {_MAX_TEMP_CEILING}, got {val}")

    # --- feedrate ---
    fr = profile["max_feedrate"]
    if not isinstance(fr, (int, float)):
        errors.append(f"max_feedrate must be a number, got {type(fr).__name__}")
    elif not (0 <= fr <= _MAX_FEEDRATE_CEILING):
        errors.append(f"max_feedrate must be between 0 and {_MAX_FEEDRATE_CEILING}, got {fr}")

    # --- build volume ---
    bv = profile["build_volume"]
    if not isinstance(bv, list) or len(bv) != 3:
        errors.append("build_volume must be a list of 3 numbers [X, Y, Z]")
    else:
        for i, dim in enumerate(bv):
            if not isinstance(dim, (int, float)):
                errors.append(f"build_volume[{i}] must be a number, got {type(dim).__name__}")
            elif dim <= 0:
                errors.append(f"build_volume[{i}] must be positive, got {dim}")

    return errors


def _save_community_profiles() -> None:
    """Persist the community cache to ``~/.kiln/community_profiles.json``."""
    _COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    for key, sp in _community_cache.items():
        payload[key] = {
            "display_name": sp.display_name,
            "max_hotend_temp": sp.max_hotend_temp,
            "max_bed_temp": sp.max_bed_temp,
            "max_chamber_temp": sp.max_chamber_temp,
            "max_feedrate": sp.max_feedrate,
            "min_safe_z": sp.min_safe_z,
            "max_volumetric_flow": sp.max_volumetric_flow,
            "build_volume": sp.build_volume,
            "notes": sp.notes,
        }
    _COMMUNITY_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def add_community_profile(
    printer_model: str,
    profile: dict[str, Any],
    *,
    source: str = "community",
) -> None:
    """Validate and save a community-contributed safety profile.

    The profile is persisted to ``~/.kiln/community_profiles.json`` so
    it survives restarts and takes precedence over bundled profiles.

    :param printer_model: Short identifier key (e.g. ``"my_custom_printer"``).
    :param profile: Dict with at least ``max_hotend_temp``, ``max_bed_temp``,
        ``max_feedrate``, and ``build_volume``.
    :param source: Attribution tag stored in the profile notes.
    :raises ValueError: If validation fails.
    """
    _load_community()

    errors = validate_safety_profile(profile)
    if errors:
        raise ValueError(f"Invalid safety profile: {'; '.join(errors)}")

    normalised = printer_model.lower().replace("-", "_").strip()
    notes = profile.get("notes", "")
    if source and source != "community":
        notes = f"[source: {source}] {notes}".strip()
    elif not notes:
        notes = f"Community-contributed profile for {printer_model}."

    sp = SafetyProfile(
        id=normalised,
        display_name=profile.get("display_name", printer_model),
        max_hotend_temp=float(profile["max_hotend_temp"]),
        max_bed_temp=float(profile["max_bed_temp"]),
        max_chamber_temp=float(profile["max_chamber_temp"]) if profile.get("max_chamber_temp") is not None else None,
        max_feedrate=float(profile["max_feedrate"]),
        min_safe_z=float(profile.get("min_safe_z", 0.0)),
        max_volumetric_flow=float(profile["max_volumetric_flow"])
        if profile.get("max_volumetric_flow") is not None
        else None,
        build_volume=profile["build_volume"],
        notes=notes,
    )

    _community_cache[normalised] = sp
    _save_community_profiles()
    logger.info("Saved community profile '%s' (source=%s)", normalised, source)


def export_profile(printer_model: str) -> dict[str, Any]:
    """Export a safety profile as a shareable dict.

    Looks up the profile (community first, then bundled) and returns a
    plain dict suitable for JSON serialisation and sharing.

    :param printer_model: Printer model identifier.
    :raises KeyError: If no profile matches *printer_model*.
    """
    profile = get_profile(printer_model)
    result = profile_to_dict(profile)
    result.pop("id", None)  # ID is the key, not part of the shareable payload.
    return result


def list_community_profiles() -> list[str]:
    """Return model names from the user's community profile file."""
    _load_community()
    return sorted(_community_cache.keys())
