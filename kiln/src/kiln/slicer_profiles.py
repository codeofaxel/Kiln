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

    bed = settings.get("first_layer_bed_temperature") or settings.get("bed_temperature")
    hotend = settings.get("first_layer_temperature") or settings.get("temperature")

    # Both or neither -- measured, not assumed.  Slicing with a start_gcode
    # of just ``M109``: PrusaSlicer (and Orca on marlin) auto-added the
    # missing ``M190``, but Orca on a klipper profile added nothing -- the
    # bed would never heat -- and every dialect dropped its automatic
    # ``G28``.  A half-floor is worse than none, so a profile that does not
    # name both temperatures is left to the slicer entirely.
    if not (bed and hotend):
        return

    lines = [
        _START_FLOOR_BED.format(bed=str(bed).strip()),
        _START_FLOOR_HOME,
        _START_FLOOR_HOTEND.format(hotend=str(hotend).strip()),
    ]

    # ``\n`` stays escaped: PrusaSlicer reads a literal backslash-n in an INI
    # value as a newline, and a real one would end the key.  Same rule the
    # E-reset follows.
    settings["start_gcode"] = "\\n".join(lines)


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
    # After the merge: an override can switch relative-E on, or replace the
    # layer_gcode that was satisfying the rule.
    _ensure_layer_e_reset(merged)
    # Likewise after the merge, so the floor quotes the temperatures this job
    # will actually print at rather than the ones the profile shipped with.
    _ensure_start_temperatures(merged)

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


def profile_with_overrides(
    base_profile: str | None,
    overrides: dict[str, str] | None,
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

    Returns ``None`` only when there is nothing at all to say.
    """
    if not overrides:
        return base_profile

    lines: list[str] = []
    remaining = dict(overrides)
    if base_profile and os.path.isfile(base_profile):
        for raw in Path(base_profile).read_text(encoding="utf-8").splitlines():
            key = raw.split("=", 1)[0].strip() if "=" in raw else ""
            if key and key in remaining:
                lines.append(f"{key} = {remaining.pop(key)}")
            else:
                lines.append(raw)
    else:
        lines.append("# Kiln auto-generated profile: overrides only")
        lines.append("")
    lines.extend(f"{key} = {remaining[key]}" for key in sorted(remaining))

    # The same invariant the bundled resolvers apply, for the same reason:
    # this door writes an .ini too, and slice_and_print pushes
    # use_relative_e_distances=1 through it for every Bambu whose model is
    # unset or unmappable — the exact callers this helper exists to serve.
    effective = {
        raw.split("=", 1)[0].strip(): raw.split("=", 1)[1].strip()
        for raw in lines
        if "=" in raw and not raw.lstrip().startswith("#")
    }
    patched = dict(effective)
    _ensure_layer_e_reset(patched)
    _ensure_start_temperatures(patched)
    for key in ("layer_gcode", "start_gcode"):
        if patched.get(key) == effective.get(key):
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
        prefix="overrides_", suffix=".ini", delete=False,
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
    """
    try:
        from kiln_pro.bridge import pro_features
    except Exception:
        return None, "kiln-pro-not-installed"
    try:
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


def brand_overrides_for_slicer(brand_id: str) -> dict[str, str] | None:
    """Generate slicer setting overrides from a brand filament profile.

    Returns a dict of PrusaSlicer INI key-value pairs that override the
    default profile temperatures with brand-specific optimal values.
    Returns ``None`` if the brand profile is not found.

    Usage::

        overrides = brand_overrides_for_slicer("bambu_petg_cf")
        if overrides:
            ini_path = resolve_slicer_profile("bambu_a1", overrides=overrides)

    :param brand_id: Brand profile ID (e.g. ``"prusament_tpu_95a"``).
    """
    try:
        from kiln.design_intelligence import resolve_filament

        resolved = resolve_filament(brand_id)
        if not resolved.is_brand_specific:
            return None

        overrides: dict[str, str] = {
            "temperature": str(resolved.nozzle_temp_optimal_c),
            "first_layer_temperature": str(resolved.nozzle_temp_optimal_c),
            "bed_temperature": str(resolved.bed_temp_optimal_c),
            "first_layer_bed_temperature": str(resolved.bed_temp_optimal_c),
        }

        return overrides
    except Exception:
        logger.debug("brand_overrides_for_slicer failed for '%s'", brand_id)
        return None


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
    # absolute E.  The shared invariant checks before it writes.
    _ensure_layer_e_reset(merged)
    _ensure_start_temperatures(merged)

    cache_key = f"{profile.id}_mme{num_extruders}:{_settings_hash(merged)}"
    if cache_key in _temp_cache and os.path.isfile(_temp_cache[cache_key]):
        return _temp_cache[cache_key]

    ini_content = _settings_to_ini(
        merged,
        f"{profile.display_name} (AMS {num_extruders}-color)",
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
