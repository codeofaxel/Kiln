"""OrcaSlicer / BambuStudio preset emission — the second serializer.

Kiln's bundled printer settings are one flat dict per printer
(``data/slicer_profiles.json``), and :func:`kiln.slicer_profiles._settings_to_ini`
turns that dict into the PrusaSlicer ``.ini`` that ``--load`` wants.  This
module turns the SAME dict into what the other command line wants: three
standalone JSON presets — machine, process, filament — that
``--load-settings`` / ``--load-filaments`` accept.

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
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
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


@dataclass(frozen=True)
class OrcaPresets:
    """The three presets one Orca slice needs, plus where they were written."""

    machine: dict[str, Any]
    process: dict[str, Any]
    filament: dict[str, Any]
    machine_path: str | None = None
    process_path: str | None = None
    filament_path: str | None = None


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
) -> OrcaPresets:
    """Translate Kiln's flat PrusaSlicer settings into three Orca presets.

    Args:
        settings: PrusaSlicer-keyed settings, as bundled in
            ``slicer_profiles.json`` or parsed back out of a generated ini.
        name: Base name for the emitted presets.  The machine's name is what
            the process and filament declare themselves compatible with, so
            all three are derived from this one string.

    Returns:
        An :class:`OrcaPresets` with the three preset bodies filled in.
    """
    machine_name = f"{name}_machine"

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

    bed = settings.get("bed_temperature")
    bed_first = settings.get("first_layer_bed_temperature", bed)
    for plate in _PLATE_TYPES:
        if bed is not None:
            filament[f"{plate}_temp"] = _as_list(bed)
        if bed_first is not None:
            filament[f"{plate}_temp_initial_layer"] = _as_list(bed_first)

    return OrcaPresets(machine=machine, process=process, filament=filament)


def write_orca_presets(
    settings: dict[str, str],
    out_dir: str,
    *,
    name: str = "kiln",
) -> OrcaPresets:
    """Serialize *settings* to three preset files in *out_dir*.

    Returns the same :class:`OrcaPresets` with its ``*_path`` fields filled
    in, ready to be handed to ``--load-settings`` / ``--load-filaments``.
    """
    presets = settings_to_orca_presets(settings, name=name)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    paths: dict[str, str] = {}
    for kind, body in (
        ("machine", presets.machine),
        ("process", presets.process),
        ("filament", presets.filament),
    ):
        path = os.path.join(out_dir, f"{name}_{kind}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        paths[kind] = path

    logger.debug("Wrote Orca presets for %s to %s", name, out_dir)
    return replace(
        presets,
        machine_path=paths["machine"],
        process_path=paths["process"],
        filament_path=paths["filament"],
    )
