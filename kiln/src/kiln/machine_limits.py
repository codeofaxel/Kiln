"""The limits a connected printer states about itself, as a proposal for its owner.

A printer Kiln has no catalogue row for is held to the generic floor until a
person confirms limits above it (:func:`kiln.safety_profiles.set_local_printer_override`
through ``add_safety_profile``).  Finding those numbers is the slow part --
a spec page, a manual, a guess nobody should make -- and a Klipper machine
has already written them down: ``printer.cfg`` names the temperature past
which each heater faults, the travel of each axis and the velocity ceiling,
and Moonraker exposes that file as the ``configfile`` object.  This module
turns that config into the profile shape ``add_safety_profile`` takes, with
a note naming every line it rests on, so the person confirms numbers their
own machine enforces instead of typing them.

A proposal is never saved by this module and never stands in for the
person's code: it is the numbers put in front of them.  Only Klipper is
read -- a Marlin machine reports its firmware and endstops over USB
(``kiln.machine_motion``) and not its heater ceilings, and the Bambu, Prusa
Link and Elegoo backends publish nothing about their own limits -- so
:func:`limits_from_klipper_config` is the one dialect, and the adapter door
says plainly when a machine has nothing to read.
"""

from __future__ import annotations

from typing import Any

#: Fields the config can settle, in the order the note lists them.
PROPOSED_FIELDS = ("max_hotend_temp", "max_bed_temp", "max_feedrate", "build_volume")
#: Where a proposal's numbers came from, written into the override's notes
#: so every readout labels them: the machine's own config, not Kiln's.
SOURCE_LABEL = "read from this printer's own Klipper configuration"


def _section(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    for key, value in config.items():
        if str(key).strip().lower() == name and isinstance(value, dict):
            return value
    return None


def _number(section: dict[str, Any] | None, key: str) -> float | None:
    if section is None:
        return None
    try:
        value = float(str(section.get(key, "")).strip())
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # NaN is not a limit


def _axis_travel(config: dict[str, Any], axis: str) -> tuple[float, str] | None:
    """The usable travel of one axis: ``position_max`` less any positive
    ``position_min`` (a negative minimum is overtravel past the bed, not
    bed).  ``None`` when the axis states no positive maximum."""
    section = _section(config, f"stepper_{axis}")
    top = _number(section, "position_max")
    if top is None or top <= 0:
        return None
    low = _number(section, "position_min") or 0.0
    travel = top - max(low, 0.0)
    if travel <= 0:
        return None
    seen = f"[stepper_{axis}] position_max: {top:g}"
    if low > 0:
        seen += f", position_min: {low:g}"
    return travel, seen


def limits_from_klipper_config(config: Any) -> dict[str, Any] | None:
    """A limits proposal read from Moonraker's ``configfile.config`` mapping.

    ``{"profile": {...}, "read": {field: line}, "missing": [field, ...]}``
    -- ``profile`` holds every field the config states, in the shape
    ``add_safety_profile`` takes, with ``notes`` naming the lines; ``read``
    says which line each number came from; ``missing`` lists the required
    fields the config left unstated, so a door can say what the person
    still has to supply.  ``None`` when *config* is not a config mapping
    or states none of the four.  Pure, and never raises.

    Hotend and bed ceilings are ``[extruder] max_temp`` and ``[heater_bed]
    max_temp``: the temperature at which the printer's board shuts the
    heater down.  Klipper documents that range as a safety check set "just
    wide enough" -- a fault limit, not a rating -- so the note names it as
    the shutdown point, and nothing here says what the hotend's parts can
    take (a PTFE liner is not in any config).  Feedrate is ``[printer]
    max_velocity`` (mm/s) as mm/min.  Build volume is each axis's travel.
    Chamber ceilings and flow limits are not read: no standard section
    states them.
    """
    if not isinstance(config, dict) or not config:
        return None
    profile: dict[str, Any] = {}
    read: dict[str, str] = {}

    hotend = _number(_section(config, "extruder"), "max_temp")
    if hotend is not None and hotend > 0:
        profile["max_hotend_temp"] = hotend
        read["max_hotend_temp"] = f"[extruder] max_temp: {hotend:g} (the board's shutdown point)"

    bed = _number(_section(config, "heater_bed"), "max_temp")
    if bed is not None and bed > 0:
        profile["max_bed_temp"] = bed
        read["max_bed_temp"] = f"[heater_bed] max_temp: {bed:g} (the board's shutdown point)"

    velocity = _number(_section(config, "printer"), "max_velocity")
    if velocity is not None and velocity > 0:
        profile["max_feedrate"] = velocity * 60.0
        read["max_feedrate"] = f"[printer] max_velocity: {velocity:g} mm/s"

    axes = [_axis_travel(config, axis) for axis in ("x", "y", "z")]
    if all(axis is not None for axis in axes):
        profile["build_volume"] = [axis[0] for axis in axes]  # type: ignore[index]
        read["build_volume"] = "; ".join(axis[1] for axis in axes)  # type: ignore[index]

    if not profile:
        return None
    profile["notes"] = f"{SOURCE_LABEL}: " + "; ".join(read[f] for f in PROPOSED_FIELDS if f in read) + "."
    missing = [field for field in PROPOSED_FIELDS if field not in profile]
    return {"profile": profile, "read": read, "missing": missing}


__all__ = ["PROPOSED_FIELDS", "SOURCE_LABEL", "limits_from_klipper_config"]
