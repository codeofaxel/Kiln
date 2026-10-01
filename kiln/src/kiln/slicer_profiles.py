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
from collections.abc import Iterable, Mapping
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
        if key.startswith("_"):
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
# Profile invariants
# ---------------------------------------------------------------------------

# The reset PrusaSlicer demands of a relative-E profile, and the form it
# looks for.  Measured against PrusaSlicer 2.9.4: the check is a
# whitespace-insensitive, case-insensitive search for "G92 E0" anywhere in
# layer_gcode, so "G92E0" and "g92 e0" both satisfy it.
_E_RESET = "G92 E0"
_E_RESET_NEEDLE = "g92e0"


def _ensure_layer_e_reset(settings: dict[str, str]) -> None:
    """Give a relative-E profile the per-layer E reset, in place.

    PrusaSlicer refuses to slice a Marlin-flavour profile that uses relative
    extruder addressing without resetting E at every layer.  The refusal is
    the quiet kind: it writes the reason to stderr, produces no gcode, and
    still **exits 0** — so the caller sees none of that and reports only
    "Slicer completed but output file was not created."

    Every bundled Bambu profile sets ``use_relative_e_distances=1`` and
    ``gcode_flavor=marlin``, but only ``bambu_a1`` and ``bambu_a2l`` declared
    a ``layer_gcode``.  The other seven — including the P2S — could not slice
    at all through a bundled profile.  The multi-extruder builder had been
    carrying its own copy of this rule for the AMS path, which is why an
    AMS-routed job on those machines sliced while the same printer's ordinary
    single-material job did not.

    So it lives here instead, applied at every door that turns settings into
    an ``.ini``: a profile author cannot forget it, and a tenth Bambu profile
    cannot reintroduce it.

    Absolute-E profiles are deliberately left alone.  A per-layer ``G92 E0``
    there resets the extruder counter mid-print, and the next absolute E value
    would extrude the whole layer's filament in one move.
    """
    if str(settings.get("use_relative_e_distances", "0")).strip() != "1":
        return

    existing = settings.get("layer_gcode", "")
    if _E_RESET_NEEDLE in "".join(existing.split()).lower():
        return

    # Prepend rather than replace, so a caller that set its own layer_gcode
    # (an M73 progress line, an M117 label) keeps it.  ``\n`` stays escaped:
    # PrusaSlicer reads a literal backslash-n in an INI value as a newline,
    # and a real one would end the key.
    settings["layer_gcode"] = f"{_E_RESET}\\n{existing}" if existing else _E_RESET


# The warm-up floor, and the form both slicers agree on.  Measured against
# PrusaSlicer 2.9.4 and OrcaSlicer 2.3.2 on 2026-08-27 by slicing a 10 mm cube
# from these profiles and reading the emitted start block back.
#
# Literal temperatures, not placeholders, and that is deliberate.  Kiln authors
# a profile once in PrusaSlicer's vocabulary and :mod:`kiln.slicer_orca`
# translates the KEYS -- but it passes G-code VALUES through untouched, so a
# placeholder is read by whichever slicer runs and the two spell their
# variables differently.  ``klipper_generic`` is the standing proof: its
# ``{temperature}`` is a vector in both dialects, and both refuse to slice.
# A number means the same thing to both.
_START_FLOOR_BED = "M190 S{bed}"
_START_FLOOR_HOME = "G28"
_START_FLOOR_HOTEND = "M109 S{hotend}"


def _ensure_start_temperatures(settings: dict[str, str]) -> None:
    """Give a profile with no start routine the warm-up floor, in place.

    A profile that declares no ``start_gcode`` leaves the start of the print
    entirely to the slicer's own defaults, and the two slicers disagree about
    what those are.  Measured, slicing the same profile through both command
    lines:

    * PrusaSlicer, marlin flavour, emits ``M190`` (bed, wait), ``M104``
      (hotend, set), ``G28``, then ``M109`` (hotend, wait) -- safe.  Its
      reprapfirmware flavour stops short of the ``M109``, so the one RRF
      profile gained its hotend wait from the floor on BOTH slicers.
    * OrcaSlicer, for a **klipper**-flavour profile, emits no temperature
      command at all before the first extrusion.  It homes at line 18 and
      extrudes at line 39, while ``M104``/``M140`` do not appear until line
      151.  The machine is asked to push filament through a cold nozzle.
      At that measurement thirty-one bundled profiles were klipper-flavour --
      every K1, K2, QIDI, Voron and Ender V3 Kiln ships -- and four more
      arrived the next day, covered by this floor without being touched.
    * OrcaSlicer, for a marlin-flavour profile, waits on the bed but only
      *sets* the hotend, so the first extrusion can begin while the nozzle is
      still climbing.

    So the floor is applied to every profile that does not state a start
    routine, whatever its flavour: the klipper profiles gain the temperature
    commands they had none of, and the marlin ones gain the hotend wait.

    ``G28`` is part of the floor and must be.  Both slicers stop emitting
    their own homing move the moment a custom ``start_gcode`` exists, so a
    floor of ``M190``/``M109`` alone buys a temperature guarantee at the cost
    of never homing -- the printer would start from wherever it believed it
    was.  Measured both ways; the three-line form is the one that homes.

    The order is bed to temperature, home, then bring the nozzle up and
    hold before any extrusion.  It is PrusaSlicer's own order minus one line,
    deliberately: PrusaSlicer also *sets* the hotend (``M104``) before homing
    so the nozzle warms during the ``G28``.  The floor omits that, trading a
    few seconds of overlap for a nozzle that is still cold while it travels
    over the plate and cannot ooze onto it -- the same bed/home/nozzle
    sequence most vendor ``PRINT_START`` macros use.

    A profile that *states* a ``start_gcode`` is left alone, including one
    that states the empty string.  That is not an oversight to be repaired:
    the nine Bambu profiles set it empty on purpose, because
    :mod:`kiln.printers.bambu_3mf` injects Bambu's own initialisation into the
    3MF afterwards, and a floor prepended here would fight it.  Key present is
    the author speaking; key absent is the author never having been asked.

    This is a floor, not a vendor start routine.  It does not heat a chamber,
    run a bed mesh, or purge -- those live in the manufacturer's own macro and
    Kiln cannot know whether the machine in front of it defines one.  What it
    guarantees is the part that is unsafe to get wrong.
    """
    if "start_gcode" in settings:
        return
    floor = start_floor(settings)
    if floor is not None:
        settings["start_gcode"] = floor


def start_floor(settings: Mapping[str, str]) -> str | None:
    """The warm-up floor :func:`_ensure_start_temperatures` writes for *settings*.

    A pure function of the temperatures, so a later layer that changes them
    (a declared material, at the slicing chokepoint) can tell the floor from
    an author's own start block -- the floor is exactly this text -- and
    write it again at the new temperatures rather than leave the printer
    heating for the old material.  ``None`` when the settings do not name
    both temperatures.
    """
    bed = settings.get("first_layer_bed_temperature") or settings.get("bed_temperature")
    hotend = settings.get("first_layer_temperature") or settings.get("temperature")

    # Both or neither -- measured, not assumed.  Slicing with a start_gcode
    # of just ``M109``: PrusaSlicer (and Orca on marlin) auto-added the
    # missing ``M190``, but Orca on a klipper profile added nothing -- the
    # bed would never heat -- and every dialect dropped its automatic
    # ``G28``.  A half-floor is worse than none, so a profile that does not
    # name both temperatures is left to the slicer entirely.
    if not (bed and hotend):
        return None

    lines = [
        _START_FLOOR_BED.format(bed=str(bed).strip()),
        _START_FLOOR_HOME,
        _START_FLOOR_HOTEND.format(hotend=str(hotend).strip()),
    ]

    # ``\n`` stays escaped: PrusaSlicer reads a literal backslash-n in an INI
    # value as a newline, and a real one would end the key.  Same rule the
    # E-reset follows.
    return "\\n".join(lines)


#: PrusaSlicer's documented base for each speed it accepts as a percentage:
#: ``"50%"`` is half of THIS key's speed.  Every other speed key is mm/s only.
_SPEED_PERCENT_BASE: dict[str, str] = {
    "external_perimeter_speed": "perimeter_speed",
    "small_perimeter_speed": "perimeter_speed",
    "solid_infill_speed": "infill_speed",
    "top_solid_infill_speed": "solid_infill_speed",
}

#: Gap fill and small perimeters (holes and bosses, radius 6.5 mm or less)
#: print at this share of the slower wall speed: both are short, tight moves
#: the slicer's own help says to keep slow.
_SHORT_MOVE_SHARE_OF_EXTERNAL = 0.5


def speed_mm_s(settings: Mapping[str, str], key: str) -> float | None:
    """The speed *settings* state for *key*, in mm/s, or ``None``.

    A percentage is read against the key PrusaSlicer documents as its base,
    so ``external_perimeter_speed = 50%`` is half the perimeter speed.
    ``None`` when the key is unstated, unreadable, zero (the slicer's
    "auto"), or a percentage of something unstated: a derivation needs a
    speed somebody chose.
    """
    raw = str(settings.get(key, "")).strip()
    if not raw:
        return None
    try:
        if raw.endswith("%"):
            base_key = _SPEED_PERCENT_BASE.get(key)
            base = speed_mm_s(settings, base_key) if base_key else None
            value = None if base is None else float(raw[:-1]) / 100.0 * base
        else:
            value = float(raw)
    except ValueError:
        return None
    return value if value is not None and value > 0 else None


def _speed_value(value: float) -> str:
    """A speed as an INI value: ``200``, ``67.5``."""
    return f"{round(value, 2):g}"


def _ensure_speed_coverage(settings: dict[str, str]) -> None:
    """Give every feature the profile's pace instead of the slicer's, in place.

    A bundled profile states five speeds -- perimeters, the outer wall,
    sparse infill, the first layer and travel -- and PrusaSlicer fills every
    speed it was not given from its own defaults: 20 mm/s for solid infill,
    15 for the top surface, 20 for gap fill, 15 for small perimeters.  Those
    suit a 60 mm/s machine.  Under a profile that prints sparse infill at
    250 they are a floor of a twelfth of that, and the floor is real: the
    feedrates are written into the G-code, so every part's top, bottom and
    solid layers print at 15-20 mm/s, and the time estimate grows with them.
    Measured 2026-09-30 through ``bambu_a1`` on an 80 x 55 x 28 mm enclosure
    in PETG: 2h28m as emitted, 1h13m with the speeds derived here.

    Each missing speed is tied to the nearest feature the author did state,
    and never runs faster than that feature:

    * solid infill -- the slower of the perimeter and sparse-infill speeds;
    * the top surface -- the outer-wall speed, no faster than solid infill;
    * gap fill and small perimeters -- half the slower of the two wall speeds.

    Printer makers' own presets hold the same relationships, and in most of
    them each of these features runs at the derived speed or faster, so a
    derived speed is never the aggressive choice.  Flow is bounded
    separately, by :func:`_ensure_flow_ceiling`: the slicer's widest solid
    lines run wider than its widest walls, so a speed that suits the walls
    can ask more of the hotend than they do.

    A stated key is the author speaking and is never replaced -- an override
    wins, and so does a profile that states the slicer's own value on
    purpose.  A profile that states no anchor speed is left to the slicer:
    its defaults at least agree with each other.
    """
    perimeter = speed_mm_s(settings, "perimeter_speed")
    external = speed_mm_s(settings, "external_perimeter_speed")
    infill = speed_mm_s(settings, "infill_speed")

    if "solid_infill_speed" not in settings and perimeter and infill:
        settings["solid_infill_speed"] = _speed_value(min(perimeter, infill))
    solid = speed_mm_s(settings, "solid_infill_speed")

    if external:
        top = min(external, solid) if solid else external
        # The slower wall: a material or a caller that slows the inner walls
        # and leaves the outer one alone must not be outrun by a hole.
        slower_wall = min(external, perimeter) if perimeter else external
        short_move = slower_wall * _SHORT_MOVE_SHARE_OF_EXTERNAL
        settings.setdefault("top_solid_infill_speed", _speed_value(top))
        settings.setdefault("gap_fill_speed", _speed_value(short_move))
        settings.setdefault("small_perimeter_speed", _speed_value(short_move))


def _ensure_flow_ceiling(settings: dict[str, str], profile_id: str) -> None:
    """Hand the slicer the hotend's flow ceiling for *profile_id*, in place.

    A speed times a line's cross-section is a flow, and a hotend melts only
    so much plastic a second.  Kiln's safety profile states that ceiling for
    each printer; nothing passed it to the slicer, whose default is no limit,
    so any speed -- stated, derived, or a caller's override at a thicker
    layer -- could ask for more.  Measured 2026-09-30 from the extruder's own
    E values, slicing an 80 x 55 x 28 mm enclosure through every bundled
    profile: the derived speeds took the fastest flow on the Bambu-class
    profiles from 20.4 to 21.4 mm³/s, under every stated ceiling, while
    ``aon_m2_plus``'s own sparse infill already asked for 36.6 against its
    ceiling of 30.  With the ceiling stated, that profile's feedrates were
    the only ones to change.  Where a profile and its ceiling disagree, the
    ceiling wins until one of them is re-read: it is the conservative of
    the two.

    Read through :func:`kiln.safety_profiles.get_profile`, the one door for
    printer limits, so a declared hotend variant or an owner's tightened
    ceiling is honoured here as everywhere else.  A value the settings state
    -- a caller's override -- is theirs; a printer with no stated ceiling is
    left without one.
    """
    if "max_volumetric_speed" in settings:
        return
    from kiln.safety_profiles import get_profile

    try:
        ceiling = get_profile(profile_id).max_volumetric_flow
    except KeyError:
        return
    if ceiling:
        settings["max_volumetric_speed"] = f"{ceiling:g}"


#: Every acceleration a profile or a door can state for a role.
_ROLE_ACCELERATIONS: tuple[str, ...] = (
    "default_acceleration",
    "perimeter_acceleration",
    "external_perimeter_acceleration",
    "infill_acceleration",
    "solid_infill_acceleration",
    "top_solid_infill_acceleration",
    "first_layer_acceleration",
    "first_layer_acceleration_over_raft",
    "bridge_acceleration",
    "travel_acceleration",
    "travel_short_distance_acceleration",
    "wipe_tower_acceleration",
)

#: PrusaSlicer machine limit <- what the start sequence states.
_ESTIMATE_MOTION_KEYS: dict[str, str] = {
    "machine_max_acceleration_x": "max_accel_x",
    "machine_max_acceleration_y": "max_accel_y",
    "machine_max_feedrate_x": "max_feedrate_x",
    "machine_max_feedrate_y": "max_feedrate_y",
    "machine_max_jerk_x": "jerk_x",
    "machine_max_jerk_y": "jerk_y",
}


def _ensure_estimate_motion(settings: dict[str, str], profile_id: str) -> None:
    """Estimate at the acceleration the machine will really run, in place.

    PrusaSlicer times a print against machine limits it uses for the
    estimate only -- never written into the G-code -- and with none stated
    it assumes 1500 mm/s².  A Bambu print Kiln slices carries no
    acceleration command of its own and is wrapped after the maker's start
    sequence, so it runs at whatever that sequence set: 6000 on the A1,
    10000 (or 5000 after flow calibration) on the X1C.  Measured 2026-09-30
    on an 80 x 55 x 28 mm enclosure through ``bambu_a1``: 1h19m estimated
    at 1500, 1h05m at the sequence's own 6000.

    The limits come from :func:`kiln.printers.bambu_3mf.start_sequence_motion`,
    which reads the same sequence the wrap sends.  The working acceleration
    is raised to any role acceleration the settings state, because those
    are written into the print and the machine runs them.  A printer with no
    start sequence of its own is left at the slicer's default; so is a
    caller that states any limit of its own, and a profile that writes its
    limits into the G-code, where a number from here would reach the
    machine.
    """
    if str(settings.get("machine_limits_usage", "")).strip() == "emit_to_gcode":
        return
    stated = (
        "machine_max_acceleration_extruding",
        "machine_max_acceleration_travel",
        *_ESTIMATE_MOTION_KEYS,
    )
    if any(key in settings for key in stated):
        return
    from kiln.printers.bambu_3mf import start_sequence_motion

    nozzle = str(settings.get("nozzle_diameter", "0.4")).replace(";", ",").split(",")[0]
    motion = start_sequence_motion(profile_id, nozzle)
    if not motion:
        return

    def _limit(value: float) -> str:
        # PrusaSlicer reads machine limits as "normal,stealth".
        return f"{value:g},{value:g}"

    accel = motion.get("accel")
    if accel:
        for key in _ROLE_ACCELERATIONS:
            try:
                accel = max(accel, float(str(settings.get(key, "0")).strip() or 0))
            except ValueError:
                continue
        settings["machine_max_acceleration_extruding"] = _limit(accel)
        settings["machine_max_acceleration_travel"] = _limit(accel)
    for key, source in _ESTIMATE_MOTION_KEYS.items():
        if source in motion:
            settings[key] = _limit(motion[source])


def _apply_printer_invariants(settings: dict[str, str], profile_id: str) -> None:
    """The rules that need to know the printer, then every general one."""
    _ensure_flow_ceiling(settings, profile_id)
    _ensure_estimate_motion(settings, profile_id)
    _apply_profile_invariants(settings)


def _apply_profile_invariants(settings: dict[str, str]) -> None:
    """Every rule an ``.ini`` must satisfy before a slicer reads it, in place.

    One call for every door that writes one -- the bundled resolver, the
    multi-extruder builder and the override fallback -- so a rule added here
    reaches all three, and no door can carry a copy that drifts.
    """
    _ensure_layer_e_reset(settings)
    _ensure_start_temperatures(settings)
    _ensure_speed_coverage(settings)


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
    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.append(normalised.removeprefix("creality_"))
    for candidate in candidates:
        profile = _cache.get(candidate)
        if profile is not None:
            return profile

    # Fuzzy prefix match.
    for key in _cache:
        for candidate in candidates:
            if candidate.startswith(key) or key.startswith(candidate):
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
    # After the merge: an override can switch relative-E on, replace the
    # layer_gcode that was satisfying the rule, change the temperatures the
    # start floor quotes, change a speed a derived one is tied to, or state
    # a flow ceiling or machine limits of its own.
    _apply_printer_invariants(merged, profile.id)

    # Build a cache key from the effective settings -- and from which of
    # them the caller stated, which the file records: an override that
    # happens to equal the printer's own value is still the caller's.
    stated = sorted(overrides or ())
    cache_key = f"{profile.id}:{_settings_hash(merged)}:{','.join(stated)}"
    if cache_key in _temp_cache and os.path.isfile(_temp_cache[cache_key]):
        return _temp_cache[cache_key]

    ini_content = _settings_to_ini(
        merged, profile.display_name, printer_id=profile.id, stated=stated,
    )

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


def profile_with_overrides(
    base_profile: str | None,
    overrides: dict[str, str] | None,
    *,
    prefix: str | None = None,
    stated: bool = True,
) -> str | None:
    """Return a profile path that CARRIES *overrides*, whatever the base.

    :func:`resolve_slicer_profile` merges overrides into a bundled profile,
    but it needs a printer id.  Callers reach the slicer without one more
    often than it looks: a printer whose TYPE is known while its model is
    unset or unmappable ("bambu" / "my-printer" resolve to no profile id).
    Every such caller used to drop its overrides on the floor -- including
    the three settings ``wrap_gcode_as_3mf`` requires of a Bambu slice
    (relative extrusion, empty start/end gcode), which is a wrong FILE,
    not merely untuned settings.

    So this is the fallback that keeps overrides reaching the slicer:

    * no overrides -> the base is returned untouched;
    * no base -> a partial ini of just the overrides, which PrusaSlicer
      loads over its own defaults (that IS its override mechanism, so this
      is the intended path, not a workaround);
    * a base -> its lines with the override keys replaced in place and any
      new ones appended, so an explicit profile keeps everything the
      caller chose except what was deliberately overridden.

    *prefix* names the written file (default ``overrides_``); a caller that
    derives from a bundled profile passes that profile's stem so the file
    still reads as the printer's -- slice telemetry counts by that stem.

    *stated* says whose the overrides are.  ``True`` (a caller's, or a door
    deciding for its caller) adds their keys to the file's record of stated
    keys (:class:`ProfileOrigin`), so a later layer leaves them alone;
    ``False`` is Kiln's own layers at the slicing chokepoint -- the filament
    identity and the material's settings -- which must stay replaceable by
    the next slice that names a different material.

    Returns ``None`` only when there is nothing at all to say.
    """
    if not overrides:
        return base_profile

    lines: list[str] = []
    remaining = dict(overrides)
    newly_stated = set(overrides) if stated else set()
    if base_profile and os.path.isfile(base_profile):
        origin = profile_origin(base_profile)
        for raw in Path(base_profile).read_text(encoding="utf-8").splitlines():
            key = raw.split("=", 1)[0].strip() if "=" in raw else ""
            if key and key in remaining:
                lines.append(f"{key} = {remaining.pop(key)}")
            elif origin.kiln and raw.startswith(_STATED_LINE.rstrip()):
                continue  # rewritten below, with this call's keys added
            else:
                lines.append(raw)
        if origin.kiln:
            at = 2 if len(lines) > 1 and lines[1].startswith(_PRINTER_LINE) else 1
            lines[at:at] = _origin_lines(None, origin.stated | newly_stated)
    else:
        lines.append(f"{_KILN_HEADER}overrides only")
        lines.extend(_origin_lines(None, newly_stated))
        lines.append("")
    lines.extend(f"{key} = {remaining[key]}" for key in sorted(remaining))

    # The same invariants the bundled resolvers apply, for the same reason:
    # this door writes an .ini too, and slice_and_print pushes
    # use_relative_e_distances=1 through it for every Bambu whose model is
    # unset or unmappable — the exact callers this helper exists to serve.
    # Every key an invariant adds or changes is written back, not a named
    # few: a list here is a second copy of the rules, and it drifts.
    effective = {
        raw.split("=", 1)[0].strip(): raw.split("=", 1)[1].strip()
        for raw in lines
        if "=" in raw and not raw.lstrip().startswith("#")
    }
    patched = dict(effective)
    _apply_profile_invariants(patched)
    for key in patched:
        if patched[key] == effective.get(key):
            continue
        patched_line = f"{key} = {patched[key]}"
        for idx, raw in enumerate(lines):
            if "=" in raw and raw.split("=", 1)[0].strip() == key:
                lines[idx] = patched_line
                break
        else:
            lines.append(patched_line)

    content = "\n".join(lines) + "\n"

    cache_key = f"overrides:{_settings_hash({'base': base_profile or '', 'body': content})}"
    if cache_key in _temp_cache and os.path.isfile(_temp_cache[cache_key]):
        return _temp_cache[cache_key]

    tmp_dir = os.path.join(tempfile.gettempdir(), "kiln_slicer_profiles")
    os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=tmp_dir,
        prefix=prefix or "overrides_", suffix=".ini", delete=False,
    ) as fh:
        fh.write(content)
        path = fh.name
    _temp_cache[cache_key] = path
    logger.debug("Wrote override profile (base=%s) → %s", base_profile, path)
    return path


def start_gcode_override_from_printer(
    adapter: Any,
    printer_id: str | None,
    overrides: dict[str, str] | None,
    *,
    material: str | None = None,
) -> tuple[dict[str, str] | None, str]:
    """Ask kiln-pro for a start G-code that calls the printer's OWN macro.

    The floor above (:func:`_ensure_start_temperatures`) guarantees a safe
    minimum start; what it cannot supply is the machine's own warm-up —
    the chamber heat, bed mesh and purge its Klipper config defines as a
    ``PRINT_START`` / ``START_PRINT`` macro.  Kiln is connected to the
    printer and can read that config; the logic that does so lives in
    kiln-pro, and this is its one public seam.

    Free-tier no-op: without kiln-pro installed this returns
    ``(None, "kiln-pro-not-installed")`` and the floor stands.  A returned
    patch is an ordinary ``start_gcode`` override — merged by the caller
    into the overrides it was already resolving, where the floor's
    "a stated start_gcode wins" rule makes the two mutually exclusive by
    construction.  Never raises.

    *material* is the material the slice will be told.  The macro takes the
    heat-up temperatures as arguments, decided here before the slice runs,
    so it is handed the temperatures the slice will print at
    (:func:`kiln.slicer_material.preview_material_values`) -- not the
    printer profile's, or a TPU print would warm up for PLA.
    """
    try:
        from kiln_pro.bridge import pro_features
    except Exception:
        return None, "kiln-pro-not-installed"
    try:
        from kiln.slicer_material import preview_material_values

        heat = preview_material_values(printer_id, material, overrides=overrides)
        if heat:
            overrides = {**heat, **(overrides or {})}
        return pro_features.start_gcode_override(adapter, printer_id, overrides)
    except Exception:
        logger.debug("start-gcode handoff declined", exc_info=True)
        return None, "handoff-error"


def slicer_profile_to_dict(profile: SlicerProfile) -> dict[str, Any]:
    """Serialise a :class:`SlicerProfile` to a plain dict for MCP responses."""
    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "slicer": profile.slicer,
        "notes": profile.notes,
        "settings": dict(profile.settings),
        "tier": profile.tier,
        "expanded_profile": {
            "available_with": "kiln-pro",
            "resolver": "get_printer_profile",
            "requires_tier": "pro",
            "url": "https://kiln3d.com/pricing",
        },
    }


def validate_profile_for_printer(
    profile_id: str,
    printer_model: str,
    *,
    settings: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Check if a slicer profile is compatible with a printer model.

    Compares the slicer profile's temperature settings against the printer's
    safety profile limits to catch mismatches (e.g. using a Bambu X1C profile
    on an Ender 3 whose PTFE hotend cannot handle high temps).

    :param profile_id: Slicer profile identifier (e.g. ``"bambu_x1c"``).
    :param printer_model: Registered printer model (e.g. ``"ender3"``).
    :param settings: The settings the slice will actually use -- the bundled
        profile's when omitted.  A door that took temperature overrides
        passes the file it is about to slice: judging the bundled profile
        instead passed every override, however hot (``reslice_with_overrides``
        did exactly that until 2026-10-01).
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
    settings = slicer_prof.settings if settings is None else settings
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
# Multi-extruder (AMS / MMU) profile generation
# ---------------------------------------------------------------------------

# Settings that need to be repeated N times (semicolon-joined) for multi-extruder.
_PER_EXTRUDER_KEYS: tuple[str, ...] = (
    "nozzle_diameter",
    "filament_diameter",
    "temperature",
    "first_layer_temperature",
    "retract_length",
    "retract_speed",
    "retract_lift",
    "retract_lift_above",
    "retract_lift_below",
)


def resolve_multiextruder_profile(
    printer_id: str,
    num_extruders: int = 4,
    *,
    overrides: dict[str, str] | None = None,
) -> str:
    """Write a temporary .ini profile for *printer_id* with multi-extruder support.

    Generates a PrusaSlicer-compatible INI that configures the slicer for
    multi-extruder printing.  Per-extruder settings (nozzle diameter,
    temperatures, retraction) are repeated *num_extruders* times as
    semicolon-separated values.

    .. note::
        ``single_extruder_multi_material`` is intentionally **not** set here.
        On PrusaSlicer 2.9 CLI, enabling that flag causes the slicer to produce
        an empty output file (silent failure).  Bambu AMS tool-change sequences
        (M620/M621) are injected later by :func:`~kiln.printers.bambu_3mf.build_bambu_3mf`.

    The output profile is suitable for slicing a model whose objects carry
    per-volume extruder assignments (as produced by
    :func:`~kiln.multicolor_3mf.compose_multicolor_3mf`).  The resulting
    G-code should then be wrapped with :func:`~kiln.printers.bambu_3mf.build_bambu_3mf`
    (via the ``wrap_gcode_as_3mf`` tool) to inject the Bambu AMS M620/M621
    load sequences.

    Args:
        printer_id: Printer model identifier (e.g. ``"bambu_a1"``).
        num_extruders: Number of extruder slots (2–4 for AMS).
        overrides: Optional key-value pairs added after profile merging.

    Returns:
        Absolute path to the generated ``.ini`` file.
    """
    if num_extruders < 1 or num_extruders > 16:
        raise ValueError(f"num_extruders must be 1–16, got {num_extruders}")

    profile = get_slicer_profile(printer_id)
    merged = dict(profile.settings)

    # Expand per-extruder settings into semicolon-separated arrays.
    for key in _PER_EXTRUDER_KEYS:
        if key in merged:
            merged[key] = ";".join([merged[key]] * num_extruders)

    # Set extruder count.  Do NOT set single_extruder_multi_material=1 —
    # PrusaSlicer 2.9 CLI silently produces no output with that flag.
    # Bambu AMS purging is handled by the bambu_3mf wrapping step.
    merged["extruder_count"] = str(num_extruders)

    if overrides:
        merged.update(overrides)
    # This builder used to set layer_gcode unconditionally, which was right
    # for the Bambu profiles it is used with and wrong for anything with
    # absolute E.  The shared invariants check before they write.
    _apply_printer_invariants(merged, profile.id)

    stated = sorted(overrides or ())
    cache_key = f"{profile.id}_mme{num_extruders}:{_settings_hash(merged)}:{','.join(stated)}"
    if cache_key in _temp_cache and os.path.isfile(_temp_cache[cache_key]):
        return _temp_cache[cache_key]

    ini_content = _settings_to_ini(
        merged,
        f"{profile.display_name} (AMS {num_extruders}-color)",
        printer_id=profile.id,
        stated=stated,
    )

    tmp_dir = os.path.join(tempfile.gettempdir(), "kiln_slicer_profiles")
    os.makedirs(tmp_dir, mode=0o700, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=tmp_dir,
        prefix=f"{profile.id}_mme{num_extruders}_",
        suffix=".ini",
        delete=False,
    ) as fh:
        fh.write(ini_content)
        path = fh.name

    _temp_cache[cache_key] = path
    logger.debug(
        "Wrote multi-extruder slicer profile %s×%d → %s",
        profile.id,
        num_extruders,
        path,
    )
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_to_ini(
    settings: dict[str, str],
    header: str = "",
    *,
    printer_id: str | None = None,
    stated: Iterable[str] = (),
) -> str:
    """Convert a flat dict to PrusaSlicer INI format, with its origin on top."""
    lines = [f"{_KILN_HEADER}{header}", *_origin_lines(printer_id, stated), ""]
    for key in sorted(settings):
        lines.append(f"{key} = {settings[key]}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Where a profile came from
# ---------------------------------------------------------------------------

#: The first line of every ``.ini`` Kiln writes.  A profile without it is
#: somebody's own: every value in it is the author speaking.
_KILN_HEADER = "# Kiln auto-generated profile: "
#: The bundled printer profile the file was built from.
_PRINTER_LINE = "# kiln-printer: "
#: The keys a caller (or a door, for the caller) stated on top of it.
_STATED_LINE = "# kiln-stated: "


def _origin_lines(printer_id: str | None, stated: Iterable[str]) -> list[str]:
    lines = [f"{_PRINTER_LINE}{printer_id}"] if printer_id else []
    return [*lines, f"{_STATED_LINE}{' '.join(sorted(set(stated)))}".rstrip()]


@dataclass(frozen=True)
class ProfileOrigin:
    """Where an ``.ini`` came from, read off its first lines.

    ``kiln`` is False for a profile Kiln did not write -- the caller's own,
    every value of which they chose.  For one Kiln wrote, ``printer_id`` is
    the bundled printer profile it was built from (``None`` for an
    overrides-only file), and ``stated`` the keys a caller set on top of it:
    the values a later layer must leave alone.  The material a slice is
    declared for writes its settings at the slicing chokepoint, long after
    the doors resolved their profiles, and this is how it knows which of the
    file's numbers are the printer's defaults and which are somebody's
    decision (:mod:`kiln.slicer_material`).
    """

    kiln: bool
    printer_id: str | None = None
    stated: frozenset[str] = frozenset()


def profile_origin(path: str | None) -> ProfileOrigin:
    """Read :class:`ProfileOrigin` from *path*; an unreadable file is the caller's own."""
    if not path:
        return ProfileOrigin(kiln=False)
    try:
        with open(path, encoding="utf-8") as fh:
            head = [fh.readline().rstrip("\n") for _ in range(4)]
    except (OSError, UnicodeDecodeError):
        return ProfileOrigin(kiln=False)
    if not head[0].startswith(_KILN_HEADER):
        return ProfileOrigin(kiln=False)
    printer_id: str | None = None
    stated: frozenset[str] = frozenset()
    for line in head[1:]:
        if line.startswith(_PRINTER_LINE):
            printer_id = line[len(_PRINTER_LINE):].strip() or None
        elif line.startswith(_STATED_LINE.rstrip()):
            stated = frozenset(line[len(_STATED_LINE.rstrip()):].split())
    return ProfileOrigin(kiln=True, printer_id=printer_id, stated=stated)


def _settings_hash(settings: dict[str, str]) -> str:
    """Deterministic short hash for cache keying."""
    import hashlib

    raw = json.dumps(settings, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()
