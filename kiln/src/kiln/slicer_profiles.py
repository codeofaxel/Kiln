"""Bundled slicer profiles for per-printer G-code generation.

Ships a curated JSON database of PrusaSlicer/OrcaSlicer settings keyed
by printer model.  The settings are written to a temporary ``.ini`` file
at slicing time, so agents never need to supply or manage external
profile files.

Usage::

    from kiln.slicer_profiles import resolve_slicer_profile, list_slicer_profiles

    ini_path = resolve_slicer_profile("ender3")   # writes temp .ini
    result = slice_file("model.stl", profile=ini_path)

    profiles = list_slicer_profiles()              # ["default", "ender3", ...]
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "slicer_profiles.json"

# Reuse temp files per printer_id so we don't leak thousands of files.
_temp_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlicerProfile:
    """A printer-specific slicer configuration.

    Attributes:
        id: Short identifier (e.g. ``"ender3"``, ``"bambu_x1c"``).
        display_name: Human-readable printer name.
        slicer: Recommended slicer (``"prusaslicer"`` or ``"orcaslicer"``).
        notes: Guidance about the profile.
        settings: INI key-value pairs suitable for ``--load``.
        tier: Minimum license tier required (``"free"`` or ``"pro"``).
    """

    id: str
    display_name: str
    slicer: str
    notes: str
    settings: dict[str, str]
    tier: str = "free"


# Profile IDs available on the free tier.  Everything else requires PRO.
_FREE_PROFILES: frozenset[str] = frozenset(
    {
        "default",
        "ender3",
        "prusa_mk3s",
        "klipper_generic",
    }
)


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: dict[str, SlicerProfile] = {}
_loaded: bool = False


def _load() -> None:
    global _loaded
    if _loaded:
        return

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load slicer profiles: %s", exc)
        _loaded = True
        return

    for key, data in raw.items():
        if key == "_meta":
            continue
        try:
            tier = "free" if key in _FREE_PROFILES else "pro"
            _cache[key] = SlicerProfile(
                id=key,
                display_name=data.get("display_name", key),
                slicer=data.get("slicer", "prusaslicer"),
                notes=data.get("notes", ""),
                settings=dict(data.get("settings", {})),
                tier=tier,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed slicer profile '%s': %s", key, exc)

    _loaded = True
    logger.debug("Loaded %d slicer profiles from %s", len(_cache), _DATA_FILE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_slicer_profile(printer_id: str) -> SlicerProfile:
    """Return the slicer profile for *printer_id*.

    Falls back to ``"default"`` if no specific profile matches.

    Args:
        printer_id: Short identifier (case-insensitive, hyphens normalised).
    """
    _load()
    normalised = printer_id.lower().replace("-", "_").strip()
    profile = _cache.get(normalised)
    if profile is not None:
        return profile

    # Fuzzy prefix match.
    for key in _cache:
        if normalised.startswith(key) or key.startswith(normalised):
            return _cache[key]

    default = _cache.get("default")
    if default is not None:
        return default
    raise KeyError(f"No slicer profile for '{printer_id}' and no default available.")


def list_slicer_profiles() -> list[str]:
    """Return all available slicer profile IDs sorted alphabetically."""
    _load()
    return sorted(_cache.keys())


def resolve_slicer_profile(
    printer_id: str,
    *,
    overrides: dict[str, str] | None = None,
) -> str:
    """Write a temporary .ini profile file for *printer_id*.

    Generates a PrusaSlicer-compatible INI file from the bundled settings,
    optionally merged with *overrides* (e.g. to change layer height or
    temperature for a specific job).

    The temp file is cached per ``printer_id`` + ``overrides`` combination
    so that repeated calls don't create new files.

    Args:
        printer_id: Printer model identifier.
        overrides: Optional key-value pairs to override bundled settings.

    Returns:
        Absolute path to the generated ``.ini`` file.
    """
    profile = get_slicer_profile(printer_id)
    merged = dict(profile.settings)
    if overrides:
        merged.update(overrides)

    # Build a cache key from the effective settings.
    cache_key = f"{profile.id}:{_settings_hash(merged)}"
    if cache_key in _temp_cache and os.path.isfile(_temp_cache[cache_key]):
        return _temp_cache[cache_key]

    ini_content = _settings_to_ini(merged, profile.display_name)

    tmp_dir = os.path.join(tempfile.gettempdir(), "kiln_slicer_profiles")
    os.makedirs(tmp_dir, mode=0o700, exist_ok=True)

    # Atomic write via NamedTemporaryFile to prevent symlink attacks
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=tmp_dir,
        prefix=f"{profile.id}_",
        suffix=".ini",
        delete=False,
    ) as fh:
        fh.write(ini_content)
        path = fh.name

    _temp_cache[cache_key] = path
    logger.debug("Wrote slicer profile %s → %s", profile.id, path)
    return path


def slicer_profile_to_dict(profile: SlicerProfile) -> dict[str, Any]:
    """Serialise a :class:`SlicerProfile` to a plain dict for MCP responses."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "slicer": profile.slicer,
        "notes": profile.notes,
        "settings": dict(profile.settings),
        "tier": profile.tier,
    }


def validate_profile_for_printer(profile_id: str, printer_model: str) -> dict[str, Any]:
    """Check if a slicer profile is compatible with a printer model.

    Compares the slicer profile's temperature settings against the printer's
    safety profile limits to catch mismatches (e.g. using a Bambu X1C profile
    on an Ender 3 whose PTFE hotend cannot handle high temps).

    :param profile_id: Slicer profile identifier (e.g. ``"bambu_x1c"``).
    :param printer_model: Registered printer model (e.g. ``"ender3"``).
    :returns: Dict with ``compatible`` (bool), ``warnings`` (list[str]),
        and ``errors`` (list[str]).
    """
    from kiln.safety_profiles import get_profile as get_safety_profile

    warnings: list[str] = []
    errors: list[str] = []

    # --- Resolve slicer profile ---
    try:
        slicer_prof = get_slicer_profile(profile_id)
    except KeyError:
        return {"compatible": True, "warnings": [], "errors": []}

    # --- Resolve safety profile ---
    try:
        safety_prof = get_safety_profile(printer_model)
    except KeyError:
        warnings.append(f"No safety profile for printer model {printer_model!r} -- cannot validate temperature limits.")
        return {"compatible": True, "warnings": warnings, "errors": []}

    # --- Check 1: Profile target mismatch ---
    profile_norm = slicer_prof.id.lower().replace("-", "_")
    printer_norm = printer_model.lower().replace("-", "_")

    if (
        profile_norm != "default"
        and profile_norm != printer_norm
        and not profile_norm.startswith(printer_norm)
        and not printer_norm.startswith(profile_norm)
    ):
        # Profile target doesn't share a family prefix (e.g. "ender3" vs "ender3_s1")
        warnings.append(
            f"Slicer profile {slicer_prof.id!r} (target: {slicer_prof.display_name}) "
            f"does not match printer model {printer_model!r} "
            f"({safety_prof.display_name}). Speeds and settings may be unsuitable."
        )

    # --- Check 2: Hotend temperature ---
    settings = slicer_prof.settings
    hotend_temps: list[tuple[str, float]] = []
    for key in ("temperature", "first_layer_temperature"):
        val = settings.get(key)
        if val is not None:
            with contextlib.suppress(ValueError, TypeError):
                hotend_temps.append((key, float(val)))

    for key, temp in hotend_temps:
        if temp > safety_prof.max_hotend_temp:
            errors.append(
                f"Profile hotend temp {key}={temp}°C exceeds "
                f"{safety_prof.display_name} max hotend limit of "
                f"{safety_prof.max_hotend_temp}°C."
            )
        elif temp > safety_prof.max_hotend_temp - 10:
            warnings.append(
                f"Profile hotend temp {key}={temp}°C is within 10°C of "
                f"{safety_prof.display_name} max hotend limit "
                f"({safety_prof.max_hotend_temp}°C)."
            )

    # --- Check 3: Bed temperature ---
    bed_temps: list[tuple[str, float]] = []
    for key in ("bed_temperature", "first_layer_bed_temperature"):
        val = settings.get(key)
        if val is not None:
            with contextlib.suppress(ValueError, TypeError):
                bed_temps.append((key, float(val)))

    for key, temp in bed_temps:
        if temp > safety_prof.max_bed_temp:
            errors.append(
                f"Profile bed temp {key}={temp}°C exceeds "
                f"{safety_prof.display_name} max bed limit of "
                f"{safety_prof.max_bed_temp}°C."
            )
        elif temp > safety_prof.max_bed_temp - 10:
            warnings.append(
                f"Profile bed temp {key}={temp}°C is within 10°C of "
                f"{safety_prof.display_name} max bed limit "
                f"({safety_prof.max_bed_temp}°C)."
            )

    compatible = len(errors) == 0
    return {"compatible": compatible, "warnings": warnings, "errors": errors}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_to_ini(settings: dict[str, str], header: str = "") -> str:
    """Convert a flat dict to PrusaSlicer INI format."""
    lines = [f"# Kiln auto-generated profile: {header}", ""]
    for key in sorted(settings):
        lines.append(f"{key} = {settings[key]}")
    lines.append("")
    return "\n".join(lines)


def _settings_hash(settings: dict[str, str]) -> str:
    """Deterministic short hash for cache keying."""
    import hashlib

    raw = json.dumps(settings, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()
