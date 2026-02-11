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

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "slicer_profiles.json"

# Reuse temp files per printer_id so we don't leak thousands of files.
_temp_cache: Dict[str, str] = {}


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
    """

    id: str
    display_name: str
    slicer: str
    notes: str
    settings: Dict[str, str]


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: Dict[str, SlicerProfile] = {}
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
            _cache[key] = SlicerProfile(
                id=key,
                display_name=data.get("display_name", key),
                slicer=data.get("slicer", "prusaslicer"),
                notes=data.get("notes", ""),
                settings=dict(data.get("settings", {})),
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


def list_slicer_profiles() -> List[str]:
    """Return all available slicer profile IDs sorted alphabetically."""
    _load()
    return sorted(_cache.keys())


def resolve_slicer_profile(
    printer_id: str,
    *,
    overrides: Optional[Dict[str, str]] = None,
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
    os.makedirs(tmp_dir, exist_ok=True)

    filename = f"{profile.id}_{_settings_hash(merged)[:8]}.ini"
    path = os.path.join(tmp_dir, filename)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ini_content)

    _temp_cache[cache_key] = path
    logger.debug("Wrote slicer profile %s → %s", profile.id, path)
    return path


def slicer_profile_to_dict(profile: SlicerProfile) -> Dict[str, Any]:
    """Serialise a :class:`SlicerProfile` to a plain dict for MCP responses."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "slicer": profile.slicer,
        "notes": profile.notes,
        "settings": dict(profile.settings),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings_to_ini(settings: Dict[str, str], header: str = "") -> str:
    """Convert a flat dict to PrusaSlicer INI format."""
    lines = [f"# Kiln auto-generated profile: {header}", ""]
    for key in sorted(settings):
        lines.append(f"{key} = {settings[key]}")
    lines.append("")
    return "\n".join(lines)


def _settings_hash(settings: Dict[str, str]) -> str:
    """Deterministic short hash for cache keying."""
    import hashlib
    raw = json.dumps(settings, sort_keys=True).encode()
    return hashlib.md5(raw).hexdigest()
