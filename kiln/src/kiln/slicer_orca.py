"""OrcaSlicer / BambuStudio preset emission — the second serializer.

Kiln's bundled printer settings are one flat dict per printer
(``data/slicer_profiles.json``), and :func:`kiln.slicer_profiles._settings_to_ini`
turns that dict into the PrusaSlicer ``.ini`` that ``--load`` wants.  This
module turns the SAME dict into what the other command line wants:
standalone JSON presets — one machine, one process, and one filament per
loaded slot — that ``--load-settings`` / ``--load-filaments`` accept.  A
plain slice emits the classic three; a multicolor input emits a filament
preset per color (see the multicolor block below).

It is a serializer, not a second source of truth.  A profile is authored once,
in PrusaSlicer's vocabulary, and this file is the only place that knows how
that vocabulary is spelled on the other side.

Everything below was measured against OrcaSlicer 2.3.2 -- from 2026-08-11 on,
each later finding dated where it is stated -- by slicing from presets built
here and reading the values back out of the emitted G-code.  These findings
shape the whole design, and each one is a silent failure if you get it wrong:

1.  **Presets must be FLAT.**  Orca does not resolve ``inherits`` for a preset
    handed to it by path — it loads the file as written.  Its own system
    profiles are inheritance chains, so a system file passed straight through
    arrives mostly empty and fails validation on whatever the parent held.
    Kiln emits every key it means, and never an ``inherits``.

2.  **``from`` must be ``"system"``.**  With ``from: "User"`` the slice dies
    at ``process not compatible with printer`` no matter what the presets say
    — measured by substituting one key at a time into a known-good preset,
    where ``from`` and ``name`` were the only two that broke it.

3.  **``compatible_printers`` is matched against the machine's ``name``**, not
    the file name.  Process and filament must both name the machine, or the
    same compatibility error fires.

4.  **``use_relative_e_distances`` must be stated outright.**  Orca's default
    is relative extrusion where PrusaSlicer's is absolute, so a profile that
    simply omits the key means opposite things to the two slicers.  Left
    implicit, an absolute-E profile is validated as relative and refused for
    having no per-layer ``G92 E0`` — a profile that slices fine in PrusaSlicer
    failing here for a setting nobody wrote.

5.  **``filament_max_volumetric_speed`` must be stated outright too**, and
    for the same reason as (4): the two slicers default an omitted key to
    OPPOSITE meanings.  PrusaSlicer defaults it to ``0`` — unlimited, honour
    the profile's speeds — while Orca defaults to about 2 mm³/s, which clamps
    every extruding move to roughly a tenth of what the profile asked for.
    Measured 2026-08-27 on a real model through the bundled ``bambu_a1``
    profile: with the key absent Orca estimated 5h27m and emitted extrusion
    moves at 20-30 mm/s where the profile asks 200-250; stating ``0`` (or any
    real limit) brought the same slice to 1h52m, next to PrusaSlicer's 2h19m
    for the identical file.  This was not just a bad estimate — the G-code
    itself printed at a fraction of the intended speed.

6.  **Every speed the profile means must be mapped**, for the reason behind
    (4) and (5): an unmapped key reaches Orca as Orca's own default.
    Measured 2026-09-30 through ``bambu_a1``: internal solid infill, the top
    surface and gap fill came out at 100, 100 and 30 mm/s under a profile
    asking 250 for sparse infill.  ``_PROCESS_SPEED`` carries every speed the
    bundled profiles state or derive
    (:func:`kiln.slicer_profiles._ensure_speed_coverage`).

7.  **Acceleration, too — and here the default is written INTO the print.**
    PrusaSlicer's defaults use machine limits for the estimate only and emit
    no ``M204`` unless a profile asks, so the printer keeps the acceleration
    its own firmware or start sequence set.  Orca's defaults write generic
    limits and per-role accelerations into every file.  Measured 2026-09-30
    through ``bambu_a1``: ``M201 X1000 Y1000``, then ``M204`` at 10000, 1500,
    500, 300 and 250 by role (a Klipper-flavour profile gets
    ``SET_VELOCITY_LIMIT`` instead); stating ``emit_machine_limits_to_gcode =
    0`` and ``default_acceleration = 0`` removed every one of them and took
    the estimate from 1h31m to 1h18m.  On a Bambu the wrap keeps the
    slicer's ``M201`` after the maker's own ``M201 X12000``, so the lower
    limit was the last one the printer read.  Limits and accelerations are
    now translated when the settings state them and stated as PrusaSlicer's
    defaults when they do not.

8.  **The brim's type is always stated.**  Orca's and BambuStudio's
    ``brim_type`` defaults to ``auto_brim``, which picks its own brim
    whatever width was asked, so Kiln states the type and the brim drawn is
    the brim asked for: :func:`drawn_brim_type`.

One upstream crash is worth knowing about, and it is narrower than it looks.
OrcaSlicer 2.3.2 SIGSEGVs inside
``update_values_to_printer_extruders_for_multiple_filaments`` when it is fed
Bambu Lab's own SYSTEM presets — the multi-extruder, extruder-variant records
that symbol walks.  It is not a property of Bambu printers: presets emitted
here are flat and single-extruder, never enter that code path, and slice
normally for a Bambu machine profile (measured 2026-08-11 against a P1S
profile, matching PrusaSlicer's output on extrusion mode, temperatures and
the absence of negative-X travel).  :func:`kiln.slicer.slice_file` reports the
crash for what it is on the off chance a caller supplies such a preset
directly.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from kiln.slicer_profiles import (
    ORCA_PLATE_TYPES,
    UnsafeSettingError,
    ensure_layer_e_reset,
    judged_numbers,
    refuse_unsafe_settings,
    speed_mm_s,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PRIME_TOWER_WIDTH_MM",
    "OrcaPresets",
    "SpellingConflict",
    "drawn_brim_type",
    "in_prusaslicer_spelling",
    "ini_to_settings",
    "outside_brim_mm",
    "prusa_key_for",
    "settings_to_orca_presets",
    "write_orca_presets",
]


# ---------------------------------------------------------------------------
# Key mapping
# ---------------------------------------------------------------------------

# PrusaSlicer key -> Orca key, for values that live on the MACHINE preset and
# are a plain scalar on both sides.
_MACHINE_SCALAR: dict[str, str] = {
    "gcode_flavor": "gcode_flavor",
    "max_print_height": "printable_height",
    "use_relative_e_distances": "use_relative_e_distances",
}

# Machine keys Orca stores PER EXTRUDER, so a scalar becomes a one-item list.
_MACHINE_PER_EXTRUDER: dict[str, str] = {
    "nozzle_diameter": "nozzle_diameter",
    "retract_length": "retraction_length",
    "retract_speed": "retraction_speed",
    "deretract_speed": "deretraction_speed",
    "retract_lift": "z_hop",
}

# Machine keys carrying G-code, which needs its escaped newlines turned real.
_MACHINE_GCODE: dict[str, str] = {
    "start_gcode": "machine_start_gcode",
    "end_gcode": "machine_end_gcode",
    "layer_gcode": "layer_change_gcode",
}

# Machine limits: the same names on both sides except that PrusaSlicer's
# "feedrate" is Orca's "speed".  A PrusaSlicer vector ("1500,1250": normal
# mode, then stealth) becomes Orca's list.  Whether they reach the G-code is
# decided once, below (finding 7); either way they feed the estimate.
_MACHINE_LIMITS: dict[str, str] = {
    "machine_max_acceleration_e": "machine_max_acceleration_e",
    "machine_max_acceleration_extruding": "machine_max_acceleration_extruding",
    "machine_max_acceleration_retracting": "machine_max_acceleration_retracting",
    "machine_max_acceleration_travel": "machine_max_acceleration_travel",
    "machine_max_acceleration_x": "machine_max_acceleration_x",
    "machine_max_acceleration_y": "machine_max_acceleration_y",
    "machine_max_acceleration_z": "machine_max_acceleration_z",
    "machine_max_feedrate_e": "machine_max_speed_e",
    "machine_max_feedrate_x": "machine_max_speed_x",
    "machine_max_feedrate_y": "machine_max_speed_y",
    "machine_max_feedrate_z": "machine_max_speed_z",
    "machine_max_jerk_e": "machine_max_jerk_e",
    "machine_max_jerk_x": "machine_max_jerk_x",
    "machine_max_jerk_y": "machine_max_jerk_y",
    "machine_max_jerk_z": "machine_max_jerk_z",
    "machine_min_extruding_rate": "machine_min_extruding_rate",
    "machine_min_travel_rate": "machine_min_travel_rate",
}

# PrusaSlicer acceleration key -> (Orca key, the PrusaSlicer key a zero falls
# back to).  The fallbacks are PrusaSlicer's own documented rules: an outer
# wall with no acceleration of its own uses the perimeters', solid infill
# uses infill's, the top surface uses solid infill's, and every other role
# uses the default.  Orca's own per-role defaults differ (inner walls and
# travel at 10000, outer walls at 500), so an unstated role is written out
# with PrusaSlicer's answer rather than left for Orca to fill.
_PROCESS_ACCELERATION: dict[str, tuple[str, str]] = {
    "perimeter_acceleration": ("inner_wall_acceleration", "default_acceleration"),
    "external_perimeter_acceleration": ("outer_wall_acceleration", "perimeter_acceleration"),
    "infill_acceleration": ("sparse_infill_acceleration", "default_acceleration"),
    "solid_infill_acceleration": ("internal_solid_infill_acceleration", "infill_acceleration"),
    "top_solid_infill_acceleration": ("top_surface_acceleration", "solid_infill_acceleration"),
    "first_layer_acceleration": ("initial_layer_acceleration", "default_acceleration"),
    "bridge_acceleration": ("bridge_acceleration", "default_acceleration"),
    "travel_acceleration": ("travel_acceleration", "default_acceleration"),
}

# The machine limits Orca clamps a role's acceleration to (same name on both
# sides).
_ACCELERATION_CLAMP_LIMITS: tuple[str, ...] = (
    "machine_max_acceleration_extruding",
    "machine_max_acceleration_travel",
    "machine_max_acceleration_x",
    "machine_max_acceleration_y",
)

# PrusaSlicer key -> Orca key on the PROCESS preset.  Orca renamed most of
# these; the shape is the same scalar on both sides.
_PROCESS_SCALAR: dict[str, str] = {
    "layer_height": "layer_height",
    "first_layer_height": "initial_layer_print_height",
    "perimeters": "wall_loops",
    "top_solid_layers": "top_shell_layers",
    "bottom_solid_layers": "bottom_shell_layers",
    "fill_density": "sparse_infill_density",
    "fill_pattern": "sparse_infill_pattern",
    "first_layer_speed": "initial_layer_speed",
    "travel_speed": "travel_speed",
    "support_material": "enable_support",
    "support_material_buildplate_only": "support_on_build_plate_only",
    "dont_support_bridges": "bridge_no_support",
    # A support plan's keys (kiln-pro's plan_supports writes them through
    # reslice_with_overrides): the gap to the part, the interface layers,
    # and which extruder prints the support and its interface.
    "support_material_contact_distance": "support_top_z_distance",
    "support_material_bottom_contact_distance": "support_bottom_z_distance",
    "support_material_interface_layers": "support_interface_top_layers",
    "support_material_bottom_interface_layers": "support_interface_bottom_layers",
    "support_material_extruder": "support_filament",
    "support_material_interface_extruder": "support_interface_filament",
    "brim_width": "brim_width",
    "skirts": "skirt_loops",
    "skirt_distance": "skirt_distance",
    # The dimensional compensations, spelled Orca's way when a caller states
    # them that way.  Both are radial.
    # Measured 2026-11-02 on OrcaSlicer 2.3.2 and BambuStudio 02.08.02.61:
    # a stated 0.05 / -0.02 is written into the G-code's settings block as
    # given; unstated, both slicers write 0.
    "xy_hole_compensation": "xy_hole_compensation",
    "xy_contour_compensation": "xy_contour_compensation",
    # How thin walls are laid (kiln.slicer_profiles.WALL_MODE): Orca names
    # the generator its own way and spells the two width rules the same.
    # Measured 2026-11-05 on OrcaSlicer 2.3.2: unmapped, a stated "classic"
    # never reached the slice -- the settings block read Orca's own "arachne".
    "perimeter_generator": "wall_generator",
    "min_feature_size": "min_feature_size",
    "min_bead_width": "min_bead_width",
    # The crack the slicer fills as it cuts a layer
    # (kiln.slicer_profiles.SLICE_CLOSING_RADIUS_MM).  Same measurement:
    # unmapped, a stated 0.2 reached the slice as Orca's own 0.049.
    "slice_closing_radius": "slice_closing_radius",
}

# PrusaSlicer's one XY size compensation grows the whole part outward by the
# value: outer contours move out by it, and the walls of a hole move into the
# hole by the same amount.  Orca splits that into its two radial keys, so
# the single value is a contour compensation of the value and a hole
# compensation of its negative.  Stated only when the settings do not state
# Orca's own pair, which is the more exact statement.
_XY_SIZE_COMPENSATION = "xy_size_compensation"

# PrusaSlicer speed key -> Orca key on the PROCESS preset, written as the
# absolute mm/s the settings state (finding 6).  A percentage cannot be
# copied across: the two slicers read it against different bases -- a small
# perimeter's "50%" is of the perimeter speed in PrusaSlicer and of the outer
# wall in Orca.
_PROCESS_SPEED: dict[str, str] = {
    "perimeter_speed": "inner_wall_speed",
    "external_perimeter_speed": "outer_wall_speed",
    "infill_speed": "sparse_infill_speed",
    "solid_infill_speed": "internal_solid_infill_speed",
    "top_solid_infill_speed": "top_surface_speed",
    "gap_fill_speed": "gap_infill_speed",
    "small_perimeter_speed": "small_perimeter_speed",
}

# PrusaSlicer key -> Orca key on the FILAMENT preset.  Orca stores every
# filament value per extruder, so all of these become one-item lists.
_FILAMENT_PER_EXTRUDER: dict[str, str] = {
    "filament_diameter": "filament_diameter",
    # Spelled the same on both sides.  Orca's own presets carry it as a
    # one-item list ("filament_density": ["1.24"] in fdm_filament_pla.json,
    # OrcaSlicer 2.3.2), and with it stated the slicer writes the print's
    # weight itself -- measured 2026-09-19: ["1.27"] on a 20 mm cube gave
    # "; filament used [g] = 4.23".  Every slice arrives with one
    # (kiln.slicer_filament); a caller building presets by hand may omit
    # it, and then Orca reports 0 and the after-the-fact fill stands in.
    "filament_density": "filament_density",
    "temperature": "nozzle_temperature",
    "first_layer_temperature": "nozzle_temperature_initial_layer",
    "min_fan_speed": "fan_min_speed",
    "max_fan_speed": "fan_max_speed",
    "bridge_fan_speed": "overhang_fan_speed",
    "disable_fan_first_layers": "close_fan_the_first_x_layers",
    # Nearest equivalent, not a measured 1:1.  PrusaSlicer's fan_always_on
    # keeps the fan running below the slow-down threshold; Orca's
    # reduce_fan_stop_start_freq keeps it running rather than cycling it.
    # Same intent — "don't let the fan stop" — different mechanism.
    "fan_always_on": "reduce_fan_stop_start_freq",
    # Flow and pressure advance, as a caller states them.  PrusaSlicer's
    # extrusion multiplier is Orca's flow ratio, the same number with the
    # same meaning.  Pressure advance has no
    # PrusaSlicer key, so it is stated under Orca's own name together with
    # the flag that switches it on: measured 2026-11-02 on OrcaSlicer 2.3.2,
    # a stated value reaches the print (``M900 K0.037``) only with
    # ``enable_pressure_advance`` at 1; left unstated the flag is 0 and the
    # value is written into the settings block and nowhere else.
    "extrusion_multiplier": "filament_flow_ratio",
    "pressure_advance": "pressure_advance",
    "enable_pressure_advance": "enable_pressure_advance",
}

# Orca's own keys a caller may state in Orca's spelling (``filament_flow_ratio``,
# ``retraction_length``), and the preset each lives on with the shape it
# takes there: every key the maps above write, read back from them so the
# two cannot disagree, plus the few that exist only on Orca's side.  A
# stated key the maps do not translate is passed through to that preset as
# written, after the translated keys, so a caller's native spelling wins
# over the same setting spelled PrusaSlicer's way (finding 9: measured
# 2026-11-02 on OrcaSlicer 2.3.2, a stated ``filament_flow_ratio`` of 0.9
# reached the G-code only once passed through; before, the settings block
# read 1).
_NATIVE_SCALAR, _NATIVE_LIST, _NATIVE_GCODE, _NATIVE_LIMITS = "scalar", "list", "gcode", "limits"
_NATIVE_HOME: dict[str, tuple[str, str]] = {
    **{dst: ("machine", _NATIVE_SCALAR) for dst in _MACHINE_SCALAR.values()},
    **{dst: ("machine", _NATIVE_LIST) for dst in _MACHINE_PER_EXTRUDER.values()},
    **{dst: ("machine", _NATIVE_GCODE) for dst in _MACHINE_GCODE.values()},
    **{dst: ("machine", _NATIVE_LIMITS) for dst in _MACHINE_LIMITS.values()},
    **{dst: ("process", _NATIVE_SCALAR) for dst in _PROCESS_SCALAR.values()},
    **{dst: ("process", _NATIVE_SCALAR) for dst in _PROCESS_SPEED.values()},
    **{dst: ("process", _NATIVE_SCALAR) for dst, _fallback in _PROCESS_ACCELERATION.values()},
    **{dst: ("filament", _NATIVE_LIST) for dst in _FILAMENT_PER_EXTRUDER.values()},
    # Orca's process-level flow ratio and its own spelling of the retraction
    # a filament carries.
    "print_flow_ratio": ("process", _NATIVE_SCALAR),
    "filament_retraction_length": ("filament", _NATIVE_LIST),
}

#: The PrusaSlicer-spelled keys the maps and the rules above translate: a
#: value under one of these is handled where it is translated, never passed
#: through, even when Orca spells it the same way.
_TRANSLATED_KEYS: frozenset[str] = frozenset(
    {
        *_MACHINE_SCALAR, *_MACHINE_PER_EXTRUDER, *_MACHINE_GCODE, *_MACHINE_LIMITS,
        *_PROCESS_SCALAR, *_PROCESS_SPEED, *_PROCESS_ACCELERATION, *_FILAMENT_PER_EXTRUDER,
        "filament_max_volumetric_speed", "max_volumetric_speed", "default_acceleration",
    }
)

#: Orca key -> the PrusaSlicer key it is the spelling of: the maps above
#: read backwards (the first PrusaSlicer key to reach an Orca key names it),
#: so a caller's Orca spelling can be written for PrusaSlicer without a
#: second hand-kept list.  An Orca key no map reaches (``print_flow_ratio``,
#: ``filament_retraction_length``) has no PrusaSlicer twin.
_PRUSA_SPELLING: dict[str, str] = {}
for _forward in (
    _MACHINE_SCALAR, _MACHINE_PER_EXTRUDER, _MACHINE_GCODE, _MACHINE_LIMITS, _PROCESS_SCALAR,
    _PROCESS_SPEED, {src: dst for src, (dst, _fb) in _PROCESS_ACCELERATION.items()}, _FILAMENT_PER_EXTRUDER,
):
    for _src, _dst in _forward.items():
        _PRUSA_SPELLING.setdefault(_dst, _src)
del _forward, _src, _dst

#: Keys the maps above carry under Orca's own name because PrusaSlicer has
#: no setting for them: pressure advance and its switch, and the split XY
#: pair.  PrusaSlicer reads none of them.
_ORCA_ONLY_KEYS: frozenset[str] = frozenset(
    {"pressure_advance", "enable_pressure_advance", "xy_hole_compensation", "xy_contour_compensation"}
)


def prusa_key_for(key: str) -> str | None:
    """PrusaSlicer's key for a value stated under the Orca spelling *key*
    (``filament_flow_ratio`` is ``extrusion_multiplier``,
    ``retraction_length`` is ``retract_length``), the key itself when the
    two slicers spell it the same, or ``None`` when PrusaSlicer has no
    setting the maps translate from (``print_flow_ratio``,
    ``pressure_advance``: a key both spell the same but PrusaSlicer does
    not read is not a twin either).  Derived from the forward maps, never a
    second list."""
    twin = _PRUSA_SPELLING.get(key)
    if twin is None:
        return None
    # The same spelling on both sides is PrusaSlicer's own key -- except
    # the keys the maps carry under Orca's own name because PrusaSlicer has
    # no setting for them (_ORCA_ONLY_KEYS).
    if twin == key and key in _ORCA_ONLY_KEYS:
        return None
    return twin


class SpellingConflict(UnsafeSettingError):
    """One setting stated twice, under PrusaSlicer's spelling and Orca's,
    with two values.  Refused the way a line break is: a profile carrying
    both would hand each reader whichever it looks for."""


def _same_value(first: str, second: str) -> bool:
    """Whether two statements of one setting agree: numerically when both
    read as numbers (``330`` and ``330.0`` are one value), else as text."""
    if str(first) == str(second):
        return True
    a, b = judged_numbers(first, per_slot=True), judged_numbers(second, per_slot=True)
    return a is not None and a == b


def _spelling_of(key: str, home: str) -> str:
    """How *key* names the setting *home*: PrusaSlicer's own key, or
    OrcaSlicer's spelling of it."""
    return "PrusaSlicer's spelling" if key == home else f"OrcaSlicer's spelling of {home!r}"


def in_prusaslicer_spelling(settings: Mapping[str, str]) -> dict[str, str]:
    """*settings* with every key stated in Orca's spelling moved under its
    PrusaSlicer twin (:func:`prusa_key_for`), so one value is what every
    reader of the profile sees: the temperature check, the safety ceiling,
    the start routine and the slicer itself.  A key with no twin
    (``print_flow_ratio``, ``pressure_advance``) is kept as written.  The
    same setting stated both ways with two different values raises
    :class:`SpellingConflict`; stated both ways with one value (``330``
    and ``330.0`` are one value), it is kept once.  Order is the caller's,
    each key where its twin, or itself, first appeared.

    A conflict is between two values the CALLER stated.  A door that fills
    settings of its own (the speeds for a printer Kiln has no profile for,
    the empty start block a wrapped Bambu slice needs) translates the
    caller's settings here FIRST and fills only what is still unset, so
    its fill never contradicts a caller's Orca spelling of the same key."""
    out: dict[str, str] = {}
    spelled: dict[str, str] = {}
    for key, value in settings.items():
        twin = prusa_key_for(key)
        home = twin if twin is not None else key
        if home in out and not _same_value(out[home], value):
            first = spelled[home]
            raise SpellingConflict(
                f"The slicer settings state one setting twice and disagree: {first} = {out[home]} "
                f"({_spelling_of(first, home)}) and {key} = {value} ({_spelling_of(key, home)}), so it "
                "was refused and nothing was sliced. State it once, under either name."
            )
        if home not in out:
            spelled[home] = key
            out[home] = value
    return out


# Orca types its build plates and keeps a temperature per plate, where
# PrusaSlicer has one bed.  Every plate gets the profile's bed temperature, so
# the print is correct whichever plate the user has selected rather than
# correct only on the one Kiln happened to guess.
_PLATE_TYPES = ORCA_PLATE_TYPES

# The only two infill patterns the slicers spell differently.  Everything else
# passes through unchanged — they share the rest of the vocabulary (gyroid,
# grid, cubic, triangles, honeycomb, concentric), and Kiln's bundled profiles
# use only gyroid.
_FILL_PATTERN_ALIASES: dict[str, str] = {
    "rectilinear": "zig-zag",
    "stars": "grid",
}

# PrusaSlicer's brim types (finding 8).  Any other value, Orca's
# ``auto_brim`` / ``brim_ears`` / ``painted`` among them, means PrusaSlicer's
# default ``outer_only`` here, as it does to PrusaSlicer.
_BRIM_TYPES = frozenset({"no_brim", "outer_only", "inner_only", "outer_and_inner"})
_DEFAULT_BRIM_TYPE = "outer_only"

# Filament family assumed when the settings do not say.  Kiln's bundled
# profiles are printer settings and carry no filament identity of their own;
# a slice through slice_file arrives with one written on (the declared
# material, the loaded spool, or PLA -- kiln.slicer_filament), and this is
# the default for presets built straight from a bare profile.  The
# temperatures the profile DOES carry are what actually drive the print,
# and they are translated exactly.
_DEFAULT_FILAMENT_TYPE = "PLA"

# Spelled the same on both sides, but it needs stating rather than copying —
# see finding 5 in the module docstring.  ``0`` is "no volumetric limit",
# PrusaSlicer's default for the very same profiles.
_FILAMENT_MAX_VOLUMETRIC_SPEED = "filament_max_volumetric_speed"

# ---------------------------------------------------------------------------
# Multicolor (measured against OrcaSlicer 2.3.x, 2026-08-27, by slicing a
# painted three-color 3MF end to end and reading T0/T1/T2 back out of the
# G-code).  Three findings, each a hard failure when missed:
#
# 1.  One filament preset per slot.  ``--load-filaments`` takes N files
#     joined with ``;``; with one file Orca has one slot and silently
#     prints every color with T0 — the flatten this module now exists to
#     prevent.  Each preset needs ``filament_is_support`` stated: Orca
#     cross-checks the per-filament vectors and refuses with
#     "filament_is_support's count 1 not equal to filament_colour's size
#     3" when one is missing.
#
# 2.  Explicit line widths.  An MMU-only flow (the purge/transition
#     paths) defaults its width to zero, and Orca dies with
#     "Flow::spacing() produced negative spacing" — only on multicolor,
#     which is why single-filament slices of the same profile never saw
#     it.  Widths are derived from the nozzle, not hardcoded.
#
# 3.  The prime tower must be enabled AND placed.  Without explicit
#     ``wipe_tower_x``/``y`` Orca's default lands off the plate and the
#     slice fails with "found gcode in unprintable area ... error_code =
#     4".  The caller supplies a placement computed from the bed and the
#     model's bbox.
#
# 4.  A prime tower is built only from relative extrusion.  A slice that
#     places one extrudes in relative distances, reset every layer,
#     whatever the profile states: Kiln's generic profile, which a printer
#     with no profile on this computer slices with, extrudes in absolute
#     ones, and Orca refuses the tower on it.
# ---------------------------------------------------------------------------

#: Line width as a multiple of nozzle diameter — the ~105% every slicer
#: defaults to (0.42 mm on a 0.4 mm nozzle).
_LINE_WIDTH_NOZZLE_FACTOR = 1.05

#: Process keys that must all state that width for a multicolor slice.
_LINE_WIDTH_KEYS = (
    "line_width",
    "inner_wall_line_width",
    "outer_wall_line_width",
    "top_surface_line_width",
    "sparse_infill_line_width",
    "internal_solid_infill_line_width",
    "initial_layer_line_width",
    "support_line_width",
)

#: Prime (wipe) tower geometry for multicolor slices — the measured
#: working values: a 30 mm tower with 30 mm³ prime volume and a 3 mm brim.
PRIME_TOWER_WIDTH_MM = 30.0
PRIME_VOLUME_MM3 = 30.0
_PRIME_TOWER_BRIM_MM = 3.0


@dataclass(frozen=True)
class OrcaPresets:
    """The presets one Orca slice needs, plus where they were written.

    ``filaments`` / ``filament_paths`` carry one entry per filament slot;
    a single-filament slice has exactly one.  ``filament`` and
    ``filament_path`` remain the first slot, for the callers and tests
    that predate multicolor.
    """

    machine: dict[str, Any]
    process: dict[str, Any]
    filament: dict[str, Any]
    machine_path: str | None = None
    process_path: str | None = None
    filament_path: str | None = None
    filaments: tuple[dict[str, Any], ...] = ()
    filament_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.filaments:
            object.__setattr__(self, "filaments", (self.filament,))
        if not self.filament_paths and self.filament_path:
            object.__setattr__(self, "filament_paths", (self.filament_path,))


# ---------------------------------------------------------------------------
# INI parsing
# ---------------------------------------------------------------------------


def ini_to_settings(ini_path: str) -> dict[str, str]:
    """Read a Kiln-generated PrusaSlicer ``.ini`` back into a settings dict.

    The ``.ini`` is the interface every slicing door already speaks: each of
    them resolves a profile to a temp ``.ini`` path and hands that to
    :func:`kiln.slicer.slice_file`.  Reading it back is what lets the Orca
    backend serve all of those doors without one of them being told about it.

    Deliberately not :mod:`configparser`: these files are a flat
    ``key = value`` list with no sections, and their G-code values contain
    ``%`` and ``;`` which configparser reads as interpolation and comments.
    """
    from kiln.slicer_profiles import profile_lines

    settings: dict[str, str] = {}
    with open(ini_path, encoding="utf-8", newline="") as fh:
        # One line per ``\n`` only (profile_lines): a reader that also ends
        # a line at ``\x85`` or `` `` would read a setting out of the
        # middle of a value the slicer reads whole.
        for raw_line in profile_lines(fh.read()):
            line = raw_line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            settings[key.strip()] = value.strip()
    return settings


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _unescape_gcode(value: str) -> str:
    r"""Turn an INI-escaped G-code value into real lines.

    PrusaSlicer's ini format carries a multi-line start/end G-code as a single
    line with literal ``\n`` sequences, and Kiln writes them that way.  JSON
    holds real newlines, so leaving the escape in place would emit a literal
    backslash-n into the machine's start G-code.
    """
    return value.replace("\\n", "\n").replace("\\r", "")


def _acceleration(settings: dict[str, str], key: str) -> float:
    """The acceleration PrusaSlicer would use for *key*, in mm/s².

    The key's own value, else its documented fallback's (``_PROCESS_ACCELERATION``),
    down to ``default_acceleration``; ``0`` means no acceleration control.
    """
    seen: set[str] = set()
    while key not in seen:
        seen.add(key)
        try:
            value = float(str(settings.get(key, "")).strip() or 0)
        except ValueError:
            value = 0.0
        if value > 0 or key == "default_acceleration":
            return max(value, 0.0)
        key = _PROCESS_ACCELERATION.get(key, ("", "default_acceleration"))[1]
    return 0.0


def _finite_number(value: Any) -> float | None:
    """*value* as a finite number, or ``None`` (unset, blank, not a number)."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: str) -> float | None:
    """*value* as a number above zero, else ``None`` (zero means no limit)."""
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def drawn_brim_type(settings: dict[str, str]) -> str:
    """The ``brim_type`` that draws what PrusaSlicer would draw for *settings*.

    ``no_brim`` when the settings ask for no brim width (or state none --
    PrusaSlicer's default width is 0); otherwise the stated type when
    PrusaSlicer knows it, else ``outer_only``, PrusaSlicer's reading of an
    unstated or foreign type.  Never ``auto_brim``: that is the slicer
    choosing a brim nobody asked for (finding 8).  All three slicers spell
    the four types the same, so this is also what any of them draws from
    these settings.
    """
    if _positive(str(settings.get("brim_width", "0")).strip() or "0") is None:
        return "no_brim"
    stated = str(settings.get("brim_type", "")).strip().lower()
    return stated if stated in _BRIM_TYPES else _DEFAULT_BRIM_TYPE


def outside_brim_mm(settings: dict[str, str]) -> float:
    """How far past the part's outside the brim *settings* ask for reaches,
    in mm: its width when it is drawn round the outside
    (:func:`drawn_brim_type`), 0 for none or for one only inside the part's
    holes.  What the first layer occupies beyond the part itself, for every
    check that keeps something else -- the bed's edge, a prime tower -- clear
    of it."""
    if drawn_brim_type(settings) not in ("outer_only", "outer_and_inner"):
        return 0.0
    return _positive(str(settings.get("brim_width", "0")).strip()) or 0.0


def _as_list(value: str) -> list[str]:
    """One-item list, the shape Orca uses for every per-extruder value.

    A PrusaSlicer vector (``1.75,1.75,1.75,1.75`` or ``PLA;PLA``) collapses
    to its first slot: the presets emitted here are single-extruder by
    contract (the module docstring's crash is the multi-extruder path), and
    a comma-joined string inside a one-item list is a value Orca cannot
    read at all.
    """
    first = re.split(r"[,;]", str(value), maxsplit=1)[0].strip()
    return [first if first else str(value)]


def _extruding_relative(settings: dict[str, str]) -> dict[str, str]:
    """A copy of *settings* that extrudes in relative distances, reset
    every layer: what a prime tower is built from (finding 4 in the
    multicolor block above).  A layer G-code of the profile's own is kept
    after the reset."""
    relative = dict(settings)
    relative["use_relative_e_distances"] = "1"
    ensure_layer_e_reset(relative)
    return relative


def settings_to_orca_presets(
    settings: dict[str, str],
    *,
    name: str = "kiln",
    filament_colors: Sequence[str] | None = None,
    wipe_tower_xy: tuple[float, float] | None = None,
    wipe_tower_width_mm: float | None = None,
) -> OrcaPresets:
    """Translate Kiln's flat PrusaSlicer settings into Orca presets.

    Args:
        settings: PrusaSlicer-keyed settings, as bundled in
            ``slicer_profiles.json`` or parsed back out of a generated ini.
        name: Base name for the emitted presets.  The machine's name is what
            the process and filament declare themselves compatible with, so
            all three are derived from this one string.
        filament_colors: One display hex per filament SLOT.  Two-plus
            entries switch on multicolor emission: one filament preset
            per slot (identical but for name and ``filament_colour``),
            plus the prime-tower and line-width process keys the painted
            path needs — see the multicolor block comment above.  ``None``
            or a single entry emits the classic single filament.
        wipe_tower_xy: Prime tower corner position in bed coordinates,
            required in practice for multicolor (Orca's default placement
            can land off the plate and fail the slice).  Ignored for
            single-filament.
        wipe_tower_width_mm: The tower's width along X when the placement
            chose one; :data:`PRIME_TOWER_WIDTH_MM` otherwise.

    Returns:
        An :class:`OrcaPresets` with the preset bodies filled in.
    """
    # The same refusal as the .ini writer (kiln.slicer_profiles), at this
    # writer: a key or value carrying a line break is never serialized.
    refuse_unsafe_settings(settings)
    machine_name = f"{name}_machine"
    multicolor = bool(filament_colors) and len(filament_colors) >= 2
    if multicolor and wipe_tower_xy is not None:
        settings = _extruding_relative(settings)

    # --- machine -------------------------------------------------------
    machine: dict[str, Any] = {
        "type": "machine",
        "name": machine_name,
        # "system", not "User" — measured: with "User" every process is
        # rejected as incompatible regardless of its contents.
        "from": "system",
        "instantiation": "true",
        "printer_technology": "FFF",
        "printer_model": name,
        "printer_variant": str(settings.get("nozzle_diameter", "0.4")),
    }
    for src, dst in _MACHINE_SCALAR.items():
        if src in settings:
            machine[dst] = str(settings[src])
    for src, dst in _MACHINE_PER_EXTRUDER.items():
        if src in settings:
            machine[dst] = _as_list(settings[src])
    for src, dst in _MACHINE_GCODE.items():
        if src in settings:
            machine[dst] = _unescape_gcode(str(settings[src]))

    # PrusaSlicer defaults absolute-E when the key is absent; Orca defaults
    # relative.  State it either way so the two slicers cannot disagree about
    # a profile that never mentioned it.
    machine.setdefault("use_relative_e_distances", str(settings.get("use_relative_e_distances", "0")))

    if "bed_shape" in settings:
        # "0x0,220x0,220x220,0x220" -> ["0x0", "220x0", "220x220", "0x220"]
        corners = [c.strip() for c in str(settings["bed_shape"]).split(",") if c.strip()]
        if corners:
            machine["printable_area"] = corners

    # Finding 7: limits the settings state are translated, and they reach the
    # G-code only when the settings say so -- PrusaSlicer's default.  Stated
    # either way, because Orca's own default writes its generic ones.
    for src, dst in _MACHINE_LIMITS.items():
        if src in settings:
            machine[dst] = [p.strip() for p in str(settings[src]).split(",") if p.strip()]
    usage = str(settings.get("machine_limits_usage", "")).strip()
    machine["emit_machine_limits_to_gcode"] = "1" if usage == "emit_to_gcode" else "0"

    # --- process -------------------------------------------------------
    process: dict[str, Any] = {
        "type": "process",
        "name": f"{name}_process",
        "from": "system",
        "instantiation": "true",
        "compatible_printers": [machine_name],
        "compatible_printers_condition": "",
    }
    for src, dst in _PROCESS_SCALAR.items():
        if src in settings:
            process[dst] = str(settings[src])
    # Stated on every slice, brim or not: left out, Orca's auto brim decides.
    process["brim_type"] = drawn_brim_type(settings)
    # PrusaSlicer's threshold 0 means "detect the angle"; Orca has no such
    # setting, so only a stated angle crosses and 0 leaves Orca's own.
    threshold = _positive(str(settings.get("support_material_threshold", "")))
    if threshold:
        process["support_threshold_angle"] = f"{threshold:g}"
    # PrusaSlicer's support STYLE is Orca's support TYPE: organic supports
    # are Orca's tree supports; snug and grid are its normal supports.
    # PrusaSlicer's support_material_auto off is Orca's manual: supports
    # only where they are painted.
    style = str(settings.get("support_material_style", "")).strip().lower()
    painted_only = str(settings.get("support_material_auto", "")).strip() == "0"
    if style or painted_only:
        shape = "tree" if style == "organic" else "normal"
        process["support_type"] = f"{shape}({'manual' if painted_only else 'auto'})"
    for src, dst in _PROCESS_SPEED.items():
        raw = str(settings.get(src, "")).strip()
        if raw.endswith("%"):
            resolved = speed_mm_s(settings, src)
            if resolved is None:
                # A percentage of a speed nobody stated: Orca's own value
                # stands rather than a number made up here.
                continue
            raw = f"{round(resolved, 2):g}"
        if raw:
            process[dst] = raw

    # Finding 7 again: "0" when the settings state no acceleration, which is
    # PrusaSlicer's own default and leaves the printer's acceleration alone.
    # Once a default is stated, every role is written out, so none of them
    # falls through to Orca's per-role numbers.
    default_accel = _acceleration(settings, "default_acceleration")
    process["default_acceleration"] = f"{default_accel:g}"
    if default_accel:
        role_accels = {src: _acceleration(settings, src) for src in _PROCESS_ACCELERATION}
        for src, (dst, _fallback) in _PROCESS_ACCELERATION.items():
            process[dst] = f"{role_accels[src]:g}"
        # Orca clamps every M204 to its machine limits even when it does not
        # emit them -- measured: 5500 and 8000 came out as 1500, its default,
        # where PrusaSlicer wrote them as asked.  A limit the settings do not
        # state is set to the highest acceleration they ask for, so the clamp
        # changes nothing; the printer's firmware stays the real ceiling.
        ceiling = f"{max(default_accel, *role_accels.values()):g}"
        for key in _ACCELERATION_CLAMP_LIMITS:
            if key not in settings:
                machine[key] = [ceiling, ceiling]

    if "sparse_infill_pattern" in process:
        pattern = process["sparse_infill_pattern"]
        process["sparse_infill_pattern"] = _FILL_PATTERN_ALIASES.get(pattern, pattern)

    # PrusaSlicer's single XY compensation, as Orca's pair, unless the
    # settings state the pair themselves (see _XY_SIZE_COMPENSATION).
    single = _finite_number(settings.get(_XY_SIZE_COMPENSATION))
    if single is not None:
        process.setdefault("xy_contour_compensation", f"{single:g}")
        process.setdefault("xy_hole_compensation", f"{-single:g}")

    if multicolor:
        # Explicit line widths: the MMU-only flow defaults to zero width
        # and kills the slice (finding 2 in the multicolor block above).
        try:
            nozzle = float(settings.get("nozzle_diameter", "0.4"))
        except (TypeError, ValueError):
            nozzle = 0.4
        width = f"{nozzle * _LINE_WIDTH_NOZZLE_FACTOR:.2f}"
        for key in _LINE_WIDTH_KEYS:
            process.setdefault(key, width)
        # Prime tower — enabled ONLY together with a placement (finding
        # 3).  Both halves measured: with a placement the slice is clean;
        # enabled without one, Orca's default spot can sit off the plate
        # and fail the whole slice; and omitted entirely the file still
        # slices with every color intact (3 tools, 232 changes), just
        # purging into the object rather than a tower.  So a caller that
        # cannot say where the tower goes gets the colors it asked for
        # instead of an error about an area it never chose.
        if wipe_tower_xy is not None:
            process["enable_prime_tower"] = "1"
            process["prime_tower_width"] = f"{wipe_tower_width_mm or PRIME_TOWER_WIDTH_MM:g}"
            process["prime_volume"] = f"{PRIME_VOLUME_MM3:g}"
            process["prime_tower_brim_width"] = f"{_PRIME_TOWER_BRIM_MM:g}"
            process["wipe_tower_x"] = f"{wipe_tower_xy[0]:.2f}"
            process["wipe_tower_y"] = f"{wipe_tower_xy[1]:.2f}"

    # --- filament ------------------------------------------------------
    # The settings' own type when stated (PrusaSlicer's ";"-joined vector,
    # first slot), else the default.
    filament_type = (
        str(settings.get("filament_type", "")).split(";")[0].split(",")[0].strip()
        or _DEFAULT_FILAMENT_TYPE
    )
    filament: dict[str, Any] = {
        "type": "filament",
        "name": f"{name}_filament",
        "from": "system",
        "instantiation": "true",
        "filament_type": [filament_type],
        "compatible_printers": [machine_name],
        "compatible_printers_condition": "",
    }
    for src, dst in _FILAMENT_PER_EXTRUDER.items():
        if src in settings:
            filament[dst] = _as_list(settings[src])
    # A pressure advance stated without its switch is asked for: the value
    # reaches the print only with the switch on (measured 2026-11-02,
    # OrcaSlicer 2.3.2: unstated, the flag is 0 and the value sits in the
    # settings block alone), so the switch is turned on for the slice.  A
    # switch the settings state is theirs either way.
    if "pressure_advance" in settings and "enable_pressure_advance" not in settings:
        filament["enable_pressure_advance"] = ["1"]

    # Stated outright, exactly like use_relative_e_distances above and for
    # the same reason: omitted, this key means "unlimited" to PrusaSlicer
    # and "about 2 mm³/s" to Orca, which throttles the print to a tenth of
    # the profile's speeds.  A profile that names a limit gets that limit;
    # one that says nothing gets 0, which is what PrusaSlicer has always
    # done with these same profiles — so the two slicers agree instead of
    # silently disagreeing, and no material figure is invented here.
    #
    # PrusaSlicer also takes a print-level ceiling -- the hotend's, which
    # kiln.slicer_profiles._ensure_flow_ceiling states -- and uses the lower
    # of the two.  Orca has only the filament's, so it carries that lower
    # value.
    limits = [
        value
        for key in (_FILAMENT_MAX_VOLUMETRIC_SPEED, "max_volumetric_speed")
        if (value := _positive(_as_list(settings.get(key, "0"))[0])) is not None
    ]
    filament[_FILAMENT_MAX_VOLUMETRIC_SPEED] = [f"{min(limits):g}" if limits else "0"]

    bed = settings.get("bed_temperature")
    bed_first = settings.get("first_layer_bed_temperature", bed)
    for plate in _PLATE_TYPES:
        if bed is not None:
            filament[f"{plate}_temp"] = _as_list(bed)
        if bed_first is not None:
            filament[f"{plate}_temp_initial_layer"] = _as_list(bed_first)

    # --- keys stated in Orca's own spelling (_NATIVE_HOME) --------------
    # Last, so a caller's native spelling wins over the translated one.  A
    # key the maps translate from is theirs, whatever it is also spelled on
    # this side (nozzle_diameter, pressure_advance, the volumetric limit).
    bodies = {"machine": machine, "process": process, "filament": filament}
    for key, value in settings.items():
        home = _NATIVE_HOME.get(key)
        if home is None or key in _TRANSLATED_KEYS:
            continue
        preset, shape = home
        text = str(value)
        if shape == _NATIVE_LIST:
            bodies[preset][key] = _as_list(text)
        elif shape == _NATIVE_LIMITS:
            bodies[preset][key] = [p.strip() for p in text.split(",") if p.strip()]
        elif shape == _NATIVE_GCODE:
            bodies[preset][key] = _unescape_gcode(text)
        else:
            bodies[preset][key] = text

    if not multicolor:
        return OrcaPresets(machine=machine, process=process, filament=filament)

    # One preset per slot, identical but for identity and color.  Every
    # per-filament vector stays one item long WITHIN each file — Orca
    # sums the slots across the loaded files and cross-checks the vector
    # lengths, so each file must also state filament_is_support
    # (finding 1).
    filaments: list[dict[str, Any]] = []
    for i, color in enumerate(filament_colors, start=1):
        slot = dict(filament)
        slot["name"] = f"{name}_filament_{i}"
        slot["filament_colour"] = [str(color)]
        slot["filament_is_support"] = ["0"]
        filaments.append(slot)
    return OrcaPresets(
        machine=machine,
        process=process,
        filament=filaments[0],
        filaments=tuple(filaments),
    )


def write_orca_presets(
    settings: dict[str, str],
    out_dir: str,
    *,
    name: str = "kiln",
    filament_colors: Sequence[str] | None = None,
    wipe_tower_xy: tuple[float, float] | None = None,
    wipe_tower_width_mm: float | None = None,
) -> OrcaPresets:
    """Serialize *settings* to preset files in *out_dir*.

    Returns the same :class:`OrcaPresets` with its ``*_path`` fields filled
    in, ready to be handed to ``--load-settings`` / ``--load-filaments``.
    Multicolor (see :func:`settings_to_orca_presets`) writes one filament
    file per slot; ``filament_paths`` lists them in slot order.
    """
    presets = settings_to_orca_presets(
        settings,
        name=name,
        filament_colors=filament_colors,
        wipe_tower_xy=wipe_tower_xy,
        wipe_tower_width_mm=wipe_tower_width_mm,
    )
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    paths: dict[str, str] = {}
    for kind, body in (
        ("machine", presets.machine),
        ("process", presets.process),
    ):
        path = os.path.join(out_dir, f"{name}_{kind}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        paths[kind] = path

    filament_paths: list[str] = []
    single = len(presets.filaments) == 1
    for i, body in enumerate(presets.filaments, start=1):
        # The single-filament file keeps its historical name so nothing
        # downstream of a plain slice changes.
        suffix = "filament" if single else f"filament_{i}"
        path = os.path.join(out_dir, f"{name}_{suffix}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        filament_paths.append(path)

    logger.debug("Wrote Orca presets for %s to %s", name, out_dir)
    return replace(
        presets,
        machine_path=paths["machine"],
        process_path=paths["process"],
        filament_path=filament_paths[0],
        filament_paths=tuple(filament_paths),
    )
