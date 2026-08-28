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

Everything below was measured against OrcaSlicer 2.3.2 on 2026-08-11, by
slicing a 20 mm cube from presets built here and reading the values back out
of the emitted G-code.  Four findings shape the whole design, and each one is
a silent failure if you get it wrong:

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
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "PRIME_TOWER_WIDTH_MM",
    "OrcaPresets",
    "ini_to_settings",
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
    "perimeter_speed": "inner_wall_speed",
    "external_perimeter_speed": "outer_wall_speed",
    "infill_speed": "sparse_infill_speed",
    "first_layer_speed": "initial_layer_speed",
    "travel_speed": "travel_speed",
    "support_material": "enable_support",
    "brim_width": "brim_width",
    "skirts": "skirt_loops",
    "skirt_distance": "skirt_distance",
}

# PrusaSlicer key -> Orca key on the FILAMENT preset.  Orca stores every
# filament value per extruder, so all of these become one-item lists.
_FILAMENT_PER_EXTRUDER: dict[str, str] = {
    "filament_diameter": "filament_diameter",
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
}

# Orca types its build plates and keeps a temperature per plate, where
# PrusaSlicer has one bed.  Every plate gets the profile's bed temperature, so
# the print is correct whichever plate the user has selected rather than
# correct only on the one Kiln happened to guess.
_PLATE_TYPES = ("cool_plate", "eng_plate", "hot_plate", "textured_plate")

# The only two infill patterns the slicers spell differently.  Everything else
# passes through unchanged — they share the rest of the vocabulary (gyroid,
# grid, cubic, triangles, honeycomb, concentric), and Kiln's bundled profiles
# use only gyroid.
_FILL_PATTERN_ALIASES: dict[str, str] = {
    "rectilinear": "zig-zag",
    "stars": "grid",
}

# Filament family assumed when a profile does not say.  Kiln's profiles are
# printer settings and carry no filament identity; the temperatures they DO
# carry are what actually drive the print, and they are translated exactly.
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
_PRIME_VOLUME_MM3 = 30.0
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
    settings: dict[str, str] = {}
    with open(ini_path, encoding="utf-8") as fh:
        for raw_line in fh:
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


def _as_list(value: str) -> list[str]:
    """One-item list, the shape Orca uses for every per-extruder value."""
    return [str(value)]


def settings_to_orca_presets(
    settings: dict[str, str],
    *,
    name: str = "kiln",
    filament_colors: Sequence[str] | None = None,
    wipe_tower_xy: tuple[float, float] | None = None,
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

    Returns:
        An :class:`OrcaPresets` with the preset bodies filled in.
    """
    machine_name = f"{name}_machine"
    multicolor = bool(filament_colors) and len(filament_colors) >= 2

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
    if "sparse_infill_pattern" in process:
        pattern = process["sparse_infill_pattern"]
        process["sparse_infill_pattern"] = _FILL_PATTERN_ALIASES.get(pattern, pattern)

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
            process["prime_tower_width"] = f"{PRIME_TOWER_WIDTH_MM:g}"
            process["prime_volume"] = f"{_PRIME_VOLUME_MM3:g}"
            process["prime_tower_brim_width"] = f"{_PRIME_TOWER_BRIM_MM:g}"
            process["wipe_tower_x"] = f"{wipe_tower_xy[0]:.2f}"
            process["wipe_tower_y"] = f"{wipe_tower_xy[1]:.2f}"

    # --- filament ------------------------------------------------------
    filament: dict[str, Any] = {
        "type": "filament",
        "name": f"{name}_filament",
        "from": "system",
        "instantiation": "true",
        "filament_type": [_DEFAULT_FILAMENT_TYPE],
        "compatible_printers": [machine_name],
        "compatible_printers_condition": "",
    }
    for src, dst in _FILAMENT_PER_EXTRUDER.items():
        if src in settings:
            filament[dst] = _as_list(settings[src])

    # Stated outright, exactly like use_relative_e_distances above and for
    # the same reason: omitted, this key means "unlimited" to PrusaSlicer
    # and "about 2 mm³/s" to Orca, which throttles the print to a tenth of
    # the profile's speeds.  A profile that names a limit gets that limit;
    # one that says nothing gets 0, which is what PrusaSlicer has always
    # done with these same profiles — so the two slicers agree instead of
    # silently disagreeing, and no material figure is invented here.
    filament[_FILAMENT_MAX_VOLUMETRIC_SPEED] = _as_list(
        settings.get("filament_max_volumetric_speed", "0")
    )

    bed = settings.get("bed_temperature")
    bed_first = settings.get("first_layer_bed_temperature", bed)
    for plate in _PLATE_TYPES:
        if bed is not None:
            filament[f"{plate}_temp"] = _as_list(bed)
        if bed_first is not None:
            filament[f"{plate}_temp_initial_layer"] = _as_list(bed_first)

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
