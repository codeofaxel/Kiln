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
from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "safety_profiles.json"
_COMMUNITY_DIR = Path.home() / ".kiln"
_COMMUNITY_FILE = _COMMUNITY_DIR / "community_profiles.json"

# Validation constants
_MAX_TEMP_CEILING = 500.0
_MAX_FEEDRATE_CEILING = 50000.0  # mm/min — matches units used in all bundled profiles
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
    #: Set by an operator who has physically changed this machine (an
    #: all-metal hotend, say).  The ONLY way a profile may exceed a
    #: curated safety limit, and honoured on local installs only.
    hardware_modified: bool = False


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_cache: dict[str, SafetyProfile] = {}
_community_cache: dict[str, SafetyProfile] = {}
_loaded: bool = False
_community_loaded: bool = False

_locked_profiles: set[str] = set()
_LOCK_FILE = _COMMUNITY_DIR / "locked_profiles.json"
_locks_loaded: bool = False


def _load_locks() -> None:
    """Load the set of admin-locked profile IDs."""
    global _locks_loaded
    if _locks_loaded:
        return
    if not _LOCK_FILE.exists():
        _locks_loaded = True
        return
    try:
        data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _locked_profiles.update(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load locked profiles: %s", exc)
    _locks_loaded = True


def _save_locks() -> None:
    """Persist locked profile IDs to disk."""
    _COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(json.dumps(sorted(_locked_profiles), indent=2), encoding="utf-8")


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
                hardware_modified=bool(data.get("hardware_modified", False)),
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
    """Load community profiles from ``~/.kiln/community_profiles.json``.

    Skipped entirely on the hosted multi-tenant deploy, where the file is
    one shared ``~/.kiln`` for every customer.  A community entry
    OVERRIDES the bundled curated ceiling, so on a shared box one caller
    could raise another's ``max_hotend_temp`` — an Ender-3's bundled
    260 °C exists because of its PTFE-lined hotend, and a stranger's
    500 °C is a fume-and-fire claim that ``validate_gcode`` would then
    honour.

    Skipping is deliberately NOT the same as refusing.  The read side of
    this module feeds the G-code validator and must answer at every tier
    on every deploy — refusing here would remove the ceiling altogether,
    which is a worse outcome than the override it prevents.  Falling
    through to the bundled curated profile is strictly safer than both:
    the packaged limits are identical for every caller and are the
    numbers the validator was designed around.
    """
    global _community_loaded
    if _community_loaded:
        return

    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        _community_loaded = True
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


#: Fields where a HIGHER number is a LOOSER limit.  A community profile
#: may lower these; it may never raise them above the curated value.
_CEILING_FIELDS = (
    "max_hotend_temp",
    "max_bed_temp",
    "max_chamber_temp",
    "max_feedrate",
    "max_volumetric_flow",
)

#: Fields where a LOWER number is a looser limit — clamped the other way.
_FLOOR_FIELDS = ("min_safe_z",)


def _clamp_to_curated(
    community: SafetyProfile, curated: SafetyProfile | None
) -> SafetyProfile:
    """Let a community profile tighten a curated limit, never loosen it.

    A community entry REPLACED the curated profile wholesale, and nothing
    compared the two: ``validate_safety_profile`` only checks a flat
    absolute range, so an Ender-3 could be given a 500 C hotend ceiling
    and pass.  The curated 260 C is not a preference — it is what a
    PTFE-lined hotend tolerates before it off-gasses.

    So the merge is directional.  A community value that is more
    conservative than the curated one is honoured, because a user
    tightening their own machine's limits is exactly what this file is
    for.  A value that is less conservative is discarded in favour of
    the curated number.  That makes exceeding a curated safety limit
    structurally impossible rather than merely disallowed, whatever path
    the value arrived by — this tool, a hand-edited file, or any future
    federation that learns to write here.

    An unknown printer has no curated profile to clamp against; the
    community entry stands, having already passed the absolute-range
    validation.

    ONE deliberate exception, and only locally.  A user who has physically
    replaced a PTFE-lined hotend with an all-metal one really can run
    hotter, and this file exists partly to record that.  Such a profile
    must say so — ``hardware_modified: true`` — which turns exceeding a
    curated limit into a conscious statement about a specific machine
    rather than a number that quietly won.  It is honoured only where the
    operator IS the caller: on the hosted deploy the community overlay is
    not loaded at all, so a shared process can never take this path.
    """
    if curated is None:
        return community
    if getattr(community, "hardware_modified", False):
        logger.info(
            "community safety profile %r exceeds curated limits under a "
            "declared hardware modification: %s",
            community.id,
            community.notes or "(no note given)",
        )
        return community

    replacements: dict[str, float] = {}
    for field in _CEILING_FIELDS:
        mine, theirs = getattr(community, field, None), getattr(curated, field, None)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            if mine > theirs:
                replacements[field] = theirs
    for field in _FLOOR_FIELDS:
        mine, theirs = getattr(community, field, None), getattr(curated, field, None)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            if mine < theirs:
                replacements[field] = theirs

    if not replacements:
        return community

    logger.warning(
        "community safety profile %r tried to loosen %s beyond the curated "
        "limits; the curated values are being used instead",
        community.id,
        ", ".join(sorted(replacements)),
    )
    return _dc_replace(community, **replacements)


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
    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.append(normalised.removeprefix("creality_"))

    # Community profiles take precedence over bundled — but only in the
    # SAFE direction.  See _clamp_to_curated.
    for candidate in candidates:
        community = _community_cache.get(candidate)
        if community is not None:
            curated = next(
                (_cache[c] for c in candidates if c in _cache), None
            )
            return _clamp_to_curated(community, curated)

    for candidate in candidates:
        profile = _cache.get(candidate)
        if profile is not None:
            return profile

    # Try fuzzy prefix match (e.g. "ender-3-v2" → "ender3").  The community
    # entry goes through the SAME clamp the exact-match branch uses: a fuzzy
    # hit resolves to a community key that usually has a curated twin under
    # that very key, so there is something to clamp against and skipping it
    # would just be a second door onto the first door's bug.
    for key in _community_cache:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
                return _clamp_to_curated(_community_cache[key], _cache.get(key))
    for key in _cache:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
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


# Last-resort limits for when even the `default` profile cannot be loaded.
# These are the values of the LEAST capable machine the registry describes, so
# an unidentified printer is treated as the weakest one rather than the
# strongest.  The previous literals here were 300/130 — described in the
# docstring as "conservative generic limits" while actually being the LOOSEST
# ceiling in the file, which would have let an unknown (possibly PTFE-lined)
# hotend be driven to 300C.  Named, and named honestly, so the next reader does
# not have to guess whether the number means anything.
_UNKNOWN_PRINTER_MAX_HOTEND_C = 250.0
_UNKNOWN_PRINTER_MAX_BED_C = 100.0


def resolve_limits(printer_id: str | None = None) -> tuple:
    """Return ``(max_hotend, max_bed)`` for a printer, with fallback.

    When *printer_id* is provided, loads the matching profile.  Falls back to
    the ``default`` profile, and finally to
    ``_UNKNOWN_PRINTER_MAX_HOTEND_C`` / ``_UNKNOWN_PRINTER_MAX_BED_C`` — the
    least-capable machine in the registry — if no profile data is available at
    all.  Both limits are RATED CAPABILITY, not a guard band; safety margin is
    applied separately and by name (see ``_PTFE_SAFE_MAX``).
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
        return _UNKNOWN_PRINTER_MAX_HOTEND_C, _UNKNOWN_PRINTER_MAX_BED_C


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
# Clamping a recommendation to the machine's curated ceiling
# ---------------------------------------------------------------------------

#: Recommendation / override keys that name an ABSOLUTE temperature or flow,
#: mapped to the :class:`SafetyProfile` ceiling each must not exceed.
#:
#: Only unambiguous keys are listed.  A bare ``speed`` is deliberately absent:
#: the learning stores record it in mm/s while ``max_feedrate`` is mm/min, and
#: a clamp that guesses units is worse than no clamp.  Feedrate is already held
#: at the G-code boundary by ``validate_gcode`` and the interceptor's
#: ``max_feedrate`` rule, which see the real units.
_SETTING_CEILINGS: dict[str, str] = {
    # Hotend — slicer keys, learning-store keys, and the community corpus's
    # free-form settings dicts all land here.
    "temperature": "max_hotend_temp",
    "first_layer_temperature": "max_hotend_temp",
    "temp_tool": "max_hotend_temp",
    "tool_temp": "max_hotend_temp",
    "nozzle_temp": "max_hotend_temp",
    "nozzle_temp_c": "max_hotend_temp",
    "nozzle_temperature": "max_hotend_temp",
    "hotend_temp": "max_hotend_temp",
    "hotend_temperature": "max_hotend_temp",
    # Bed.
    "bed_temperature": "max_bed_temp",
    "first_layer_bed_temperature": "max_bed_temp",
    "temp_bed": "max_bed_temp",
    "bed_temp": "max_bed_temp",
    "bed_temp_c": "max_bed_temp",
    # Chamber.
    "chamber_temperature": "max_chamber_temp",
    "chamber_temp": "max_chamber_temp",
    "chamber_temp_c": "max_chamber_temp",
    # Volumetric flow.
    "max_volumetric_speed": "max_volumetric_flow",
    "filament_max_volumetric_speed": "max_volumetric_flow",
    "max_volumetric_speed_mm3s": "max_volumetric_flow",
}


@dataclass(frozen=True)
class ClampedSettings:
    """Result of :func:`clamp_settings_to_profile`.

    Attributes:
        settings: The settings dict, with any over-ceiling value replaced by
            the curated ceiling.  A copy; the input is never mutated.
        clamped: One human-readable line per key that was lowered, suitable
            for a ``rationale`` list or a log line.  Empty when nothing was
            over the ceiling, which is the ordinary case.
    """

    settings: dict[str, Any]
    clamped: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return bool(self.clamped)


def clamp_settings_to_profile(
    settings: dict[str, Any] | None,
    printer_id: str | None,
) -> ClampedSettings:
    """Hold a recommended or applied setting under the machine's curated ceiling.

    The read-side twin of :func:`_clamp_to_curated`.  That one stops a
    community profile from RAISING a limit; this one stops any number produced
    downstream of a limit — a community median, a learned aggregate, a replayed
    recovery fix, a calibrated flow figure — from being handed to a slicer or a
    printer ABOVE it.  Same rule, other end of the pipe: a value more
    conservative than the ceiling passes through untouched, a value less
    conservative is replaced by the ceiling.

    This never refuses.  A setting nobody has a ceiling for, an unknown
    printer, an unparseable value, or a missing profile all return the input
    unchanged — clamping down to nothing would remove the limit rather than
    enforce it, which is the failure this whole path exists to prevent.  When
    the printer IS known, its ceiling comes from :func:`get_profile`, the same
    authority ``validate_gcode`` consults, so a recommendation can never
    disagree with the enforcement that will judge it.

    A ``hardware_modified`` community profile needs no special case here: it
    has already been honoured by :func:`get_profile`, so its declared ceiling
    is simply the ceiling this clamps against.

    Args:
        settings: Recommendation / override dict.  Never mutated.
        printer_id: The machine the settings are for.  ``None`` means there is
            nothing curated to clamp against and the settings pass through.
    """
    if not isinstance(settings, dict) or not settings:
        return ClampedSettings(dict(settings) if isinstance(settings, dict) else {})
    if not printer_id:
        return ClampedSettings(dict(settings))

    try:
        profile = get_profile(printer_id)
    except KeyError:
        # No profile at all — nothing to clamp against.  Pass through rather
        # than invent a ceiling.
        return ClampedSettings(dict(settings))

    out = dict(settings)
    notes: list[str] = []
    for key, ceiling_field in _SETTING_CEILINGS.items():
        if key not in out:
            continue
        ceiling = getattr(profile, ceiling_field, None)
        if not isinstance(ceiling, (int, float)):
            continue  # e.g. a printer with no chamber, or no published flow cap
        raw = out[key]
        if isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= ceiling:
            continue
        # Keep the caller's own type — the slicer-override surface is
        # string-typed and the learning stores are numeric.  int() truncates,
        # which rounds toward the conservative side.
        if isinstance(raw, str):
            out[key] = f"{ceiling:g}"
        elif isinstance(raw, int):
            out[key] = int(ceiling)
        else:
            out[key] = ceiling
        notes.append(
            f"{key} {value:g} exceeds {profile.display_name}'s "
            f"{ceiling_field} of {ceiling:g}; using {ceiling:g}"
        )

    if notes:
        logger.warning(
            "clamped %d setting(s) to %s's curated limits: %s",
            len(notes),
            profile.id,
            "; ".join(notes),
        )
    return ClampedSettings(out, tuple(notes))


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
    - Feedrate is numeric and within ``[0, 50000]`` mm/min.
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
            "hardware_modified": sp.hardware_modified,
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
    _load_locks()

    normalised_check = printer_model.lower().replace("-", "_").strip()
    if normalised_check in _locked_profiles:
        raise ValueError(
            f"Safety profile '{printer_model}' is admin-locked. "
            f"An admin must unlock it before modifications are allowed."
        )

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
        hardware_modified=bool(profile.get("hardware_modified", False)),
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


# ---------------------------------------------------------------------------
# Lockable safety profiles (Enterprise)
# ---------------------------------------------------------------------------


def lock_safety_profile(printer_model: str) -> bool:
    """Lock a safety profile so agents cannot modify its limits.

    When locked, calls to ``add_community_profile`` for this model
    are rejected. Only an admin can unlock.

    Args:
        printer_model: Profile identifier to lock.

    Returns:
        ``True`` if the profile was locked (or was already locked).
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    _locked_profiles.add(normalised)
    _save_locks()
    logger.info("Locked safety profile '%s'", normalised)
    return True


def unlock_safety_profile(printer_model: str) -> bool:
    """Unlock a previously locked safety profile.

    Args:
        printer_model: Profile identifier to unlock.

    Returns:
        ``True`` if the profile was unlocked, ``False`` if it wasn't locked.
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    if normalised not in _locked_profiles:
        return False
    _locked_profiles.discard(normalised)
    _save_locks()
    logger.info("Unlocked safety profile '%s'", normalised)
    return True


def is_profile_locked(printer_model: str) -> bool:
    """Check whether a safety profile is admin-locked.

    Args:
        printer_model: Profile identifier.

    Returns:
        ``True`` if the profile is locked.
    """
    _load_locks()
    normalised = printer_model.lower().replace("-", "_").strip()
    return normalised in _locked_profiles


def list_locked_profiles() -> list[str]:
    """Return all admin-locked profile IDs."""
    _load_locks()
    return sorted(_locked_profiles)
