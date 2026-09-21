"""What the connected machine says about its own motion, for the session.

The catalogue's motion block (:mod:`kiln.motion_facts`) is what the vendor
published.  Some cells it leaves null on purpose -- a home spot that is a
placeholder in the reference config, a Z ceiling that differs by build, a
switch whose position the vendor never stated -- and a machine that can be
asked settles those for THIS unit, this session: a Klipper machine through
the ``configfile`` object Moonraker exposes, a Marlin machine on USB through
its M115 / M211 / M119 reports (:mod:`kiln.printers.marlin_report`).  Each cell filled
this way carries :data:`kiln.motion_facts.SOURCE_CLASS_MACHINE` as its
source, so the plan text can say which facts came off the machine; nothing
is written back.  The vendor's published fact always outranks the unit's
own -- a config may be a community edit -- and a machine that cannot be
asked leaves every refusal in place.

Read by :meth:`kiln.printers.base.PrinterAdapter._fill_motion_from_machine`.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from kiln.motion_facts import SOURCE_CLASS_MACHINE, MotionFacts, MotionSource

#: Above this fraction of ``position_max`` a physical Z endstop sits at the
#: far end of the travel: the bed at its lowest, or a CoreXZ head at its top.
#: In Klipper's frame Z is the nozzle-to-plate distance, so a home that ends
#: there ends with the nozzle as far from the plate as it can be.
_FAR_END_FRACTION = 0.75


def _cfg_section(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    for key, value in config.items():
        if str(key).strip().lower() == name and isinstance(value, dict):
            return value
    return None


def _cfg_float(section: dict[str, Any] | None, key: str) -> float | None:
    if section is None:
        return None
    try:
        return float(str(section.get(key, "")).strip())
    except (TypeError, ValueError):
        return None


def _cfg_has_prefix(config: dict[str, Any], prefix: str) -> bool:
    return any(str(key).strip().lower().startswith(prefix) for key in config)


def _far_end_method(facts: MotionFacts, line: str) -> tuple[str, str] | None:
    """A Z home that ends at the far end of the travel, named by the Z carrier.

    Shared by the Klipper and Marlin readers: nothing descends toward the
    plate, and the catalogue's own Z carrier says whether that is a bed
    dropping to a bottom switch or a head rising to a top one.  A machine
    whose carrier the catalogue does not know stays null.
    """
    if facts.z_carrier == "bed":
        return "endstop_switch_off_plate", line + " -- the bed drops to a switch at the bottom of its travel"
    if facts.z_carrier == "head":
        return "endstop_switch_top", line + " -- the head rises to a switch at the top of its travel"
    return None


def _machine_z_home_method(config: dict[str, Any], facts: MotionFacts) -> tuple[str, str] | None:
    """The Z-home method a ``printer.cfg`` states, with the line it rests on.

    A probe as the virtual endstop is an on-plate home; the section that
    defines the probe names its technology where it can.  A physical pin
    is read by where it sits: at the far end of the travel nothing
    descends toward the plate (a bed dropping to a bottom switch, a CoreXZ
    head rising to a top one) and the catalogue's own Z carrier says which
    of the two it is; anywhere else the nozzle reaches plate height, the
    refusing value.  A pin at the far end on a machine whose Z carrier the
    catalogue does not know stays null -- Kiln will not decide which part
    moved from a number alone.
    """
    stepper_z = _cfg_section(config, "stepper_z")
    if stepper_z is None:
        return None
    pin = str(stepper_z.get("endstop_pin", "")).strip()
    if not pin:
        return None
    if "z_virtual_endstop" in pin.lower():
        # The section that defines the probe names its technology.  Creality's
        # prtouch and Klipper's load_cell_probe are strain gauges behind the
        # nozzle -- the nozzle itself touches the plate; beacon, cartographer
        # and the scanner family are eddy-current; a bare [probe] says nothing.
        if _cfg_section(config, "load_cell_probe") is not None or _cfg_has_prefix(config, "prtouch"):
            return "nozzle_contact_plate", f"[stepper_z] endstop_pin: {pin}; a nozzle strain-gauge probe section defined"
        if _cfg_section(config, "bltouch") is not None:
            return "probe_touch", f"[stepper_z] endstop_pin: {pin}; [bltouch] defined"
        if any(_cfg_has_prefix(config, name) for name in ("probe_eddy_current", "beacon", "cartographer", "scanner")):
            return "probe_eddy", f"[stepper_z] endstop_pin: {pin}; an eddy-current probe section defined"
        return "probe_inductive", f"[stepper_z] endstop_pin: {pin}; [probe] defined, technology not named"
    endstop = _cfg_float(stepper_z, "position_endstop")
    z_max = _cfg_float(stepper_z, "position_max")
    if endstop is None or z_max is None or z_max <= 0:
        return None
    line = f"[stepper_z] endstop_pin: {pin}, position_endstop: {endstop:g}, position_max: {z_max:g}"
    if endstop >= _FAR_END_FRACTION * z_max:
        return _far_end_method(facts, line)
    return "endstop_switch", line + " -- the nozzle reaches plate height at the switch"


def _machine_home_xy(config: dict[str, Any]) -> tuple[tuple[float, float], str] | None:
    safe = _cfg_section(config, "safe_z_home")
    if safe is None:
        return None
    raw = str(safe.get("home_xy_position", "")).strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 2:
        return None
    try:
        xy = (float(parts[0]), float(parts[1]))
    except ValueError:
        return None
    return xy, f"[safe_z_home] home_xy_position: {raw}"


def _machine_travels_blind(config: dict[str, Any]) -> tuple[bool, str] | None:
    """Whether a bare G28 moves X/Y before Z is known, from the config alone.

    ``[safe_z_home]`` with a ``z_hop`` lifts first (Klipper applies the hop
    to every homing command); without one, or without the section, X and
    Y home at whatever height the head has.  A ``[homing_override]`` is a
    macro body Kiln does not parse, so the answer stays unknown.
    """
    if _cfg_section(config, "homing_override") is not None:
        return None
    safe = _cfg_section(config, "safe_z_home")
    if safe is None:
        return True, "no [safe_z_home] and no [homing_override]: X and Y home before Z, at the current height"
    hop = _cfg_float(safe, "z_hop") or 0.0
    if hop > 0:
        return False, f"[safe_z_home] z_hop: {hop:g} -- Z lifts before X and Y move"
    return True, "[safe_z_home] without z_hop: X and Y home before Z, at the current height"


def _machine_unhomed_policy(config: dict[str, Any]) -> tuple[str, str] | None:
    """Stock Klipper refuses a move before homing -- unless the config fakes a home.

    A ``SET_KINEMATIC_POSITION`` or ``FORCE_MOVE`` anywhere in a macro body
    (the Neptune 4 and the OrangeStorm Giga run one at boot) makes the
    machine look homed when it is not, and no enum value holds that; the
    cell stays null so the plan text never claims a refusal.
    """
    for section in config.values():
        if not isinstance(section, dict):
            continue
        body = str(section.get("gcode", "")).upper()
        if "SET_KINEMATIC_POSITION" in body or "FORCE_MOVE " in body:
            return None
    return "refused", "Klipper, and no macro fakes a homed position (no SET_KINEMATIC_POSITION or FORCE_MOVE in any gcode body)"


def fill_from_klipper_config(facts: MotionFacts, config: dict[str, Any] | None) -> MotionFacts:
    """The catalogue row with its null cells settled by this machine's own config.

    *config* is Moonraker's ``configfile.config`` mapping (one entry per
    ``printer.cfg`` section, string values).  Only null cells are touched
    -- the vendor's published fact outranks a unit's config when both
    exist, because the config may be a community edit -- and each filled
    cell carries a :data:`SOURCE_CLASS_MACHINE` source naming the lines it
    rests on.  A config that states nothing usable returns *facts*
    unchanged.
    """
    if not isinstance(config, dict) or not facts.needs_machine_fill():
        return facts
    updates: dict[str, Any] = {}
    sources = dict(facts.sources)

    def settle(name: str, value: Any, note: str) -> None:
        updates[name] = value
        sources[name] = MotionSource(source_class=SOURCE_CLASS_MACHINE, note=note)

    if facts.z_home_method is None:
        found = _machine_z_home_method(config, facts)
        if found:
            settle("z_home_method", *found)
    if facts.z_home_xy_mm is None:
        found_xy = _machine_home_xy(config)
        if found_xy:
            settle("z_home_xy_mm", *found_xy)
    if facts.home_routine_travels_blind is None:
        found_blind = _machine_travels_blind(config)
        if found_blind:
            settle("home_routine_travels_blind", *found_blind)
    if facts.unhomed_move_policy is None:
        found_policy = _machine_unhomed_policy(config)
        if found_policy:
            settle("unhomed_move_policy", *found_policy)
    if facts.z_travel_limit_mm is None:
        z_max = _cfg_float(_cfg_section(config, "stepper_z"), "position_max")
        if z_max is not None and z_max > 0:
            settle("z_travel_limit_mm", z_max, f"[stepper_z] position_max: {z_max:g}")
            settle("z_travel_limit_kind", "firmware_config", "position_max config line")
    if not updates:
        return facts
    return replace(facts, sources=sources, **updates)


# ---------------------------------------------------------------------------
# A Marlin machine's own reports
# ---------------------------------------------------------------------------



def _marlin_family(report: Any) -> tuple[str, str] | None:
    name = str(getattr(report, "firmware_name", "") or "")
    version = str(getattr(report, "firmware_version", "") or "")
    line = f"M115 FIRMWARE_NAME:{name} {version}".rstrip()
    lowered = name.lower()
    if lowered.startswith("prusa-firmware"):
        return "prusa_firmware", line
    if lowered != "marlin":
        return None
    major = version.removeprefix("bugfix-").split(".", 1)[0]
    if major == "1":
        return "marlin_1", line
    if major.isdigit() and int(major) >= 2:
        return "marlin_2", line
    if getattr(report, "marlin_2_markers", False):
        # A fork that prints no version number (Creality's 1.1.6.1 tree says
        # "Marlin Creality 3D"): a 2.x build stamp or Cap:HOST_ACTION_COMMANDS
        # is the family marker; without one the family stays unknown.
        return "marlin_2", line + " -- no version number; the 2.x build stamp or HOST_ACTION_COMMANDS capability names the family"
    return None


def _marlin_z_home_method(report: Any, facts: MotionFacts) -> tuple[str, str] | None:
    """What M119 and M115 together leave as the only reading of the Z home.

    Marlin reports which endstop PINS exist, not which one homes Z, so a
    value is set only where one reading remains, and only on a build that
    has said ``Cap:Z_PROBE:0`` -- the reader never asks M119 of a machine
    with a probe, because on a BLTouch the report moves the pin.  With no
    probe: a Z-max line and no Z-min line is a home that ends at the far end
    of the travel, named by the catalogue's Z carrier; a Z-min line is the
    switch the nozzle descends to.  A machine with a probe, or a firmware
    that reports no capabilities, keeps the refusing default: whether Z
    homes on the probe is a compile-time choice no report carries.
    """
    endstops = frozenset(getattr(report, "endstops", ()) or ())
    caps = dict(getattr(report, "capabilities", {}) or {})
    if not endstops or caps.get("Z_PROBE") is not False:
        # The reader asks M119 only of a build that said Cap:Z_PROBE:0 (the
        # report moves a BLTouch pin), so a machine with a probe -- on its
        # own pin or sharing the Z-min plug -- keeps the refusing default.
        return None
    z_min = "z_min" in endstops
    z_max = "z_max" in endstops
    seen = "M119 lists " + ", ".join(sorted(endstops)) + "; M115 Cap:Z_PROBE:0"
    if z_max and not z_min:
        return _far_end_method(facts, seen + " -- the only Z endstop is at the far end of the travel")
    if z_min:
        # A Z-min pin is listed whether or not a switch is wired; with no
        # probe at all, the switch on it is the only way this Z can home
        # downward, and a Z-max pin beside it does not prove the opposite.
        return "endstop_switch", seen + " -- the nozzle descends to the switch at plate height"
    return None


def fill_from_marlin_report(facts: MotionFacts, report: Any) -> MotionFacts:
    """The catalogue row with its null cells settled by this Marlin unit's reports.

    *report* is a :class:`kiln.printers.marlin_report.MarlinMotionReport`
    (M115 identity and capabilities, M211 soft-endstop limits, M119
    endstop list), read without moving anything.  Marlin reports far less
    than a Klipper config: the Z ceiling and the firmware family are
    settled outright, the Z-home method only where the reports leave one
    reading, and the home spot, lift order and unhomed-move policy never
    -- those are compile-time choices no read-only command carries, so
    they stay null and the gate keeps its refusing defaults.
    """
    if report is None or not facts.needs_machine_fill():
        return facts
    updates: dict[str, Any] = {}
    sources = dict(facts.sources)

    def settle(name: str, value: Any, note: str) -> None:
        updates[name] = value
        sources[name] = MotionSource(source_class=SOURCE_CLASS_MACHINE, note=note)

    z_max = getattr(report, "z_max", None)
    if facts.z_travel_limit_mm is None and isinstance(z_max, (int, float)) and z_max > 0:
        settle("z_travel_limit_mm", float(z_max), f"M211 soft endstop Max Z{z_max:g}")
        settle("z_travel_limit_kind", "firmware_config", "M211 report of the compiled Z maximum")
    if facts.firmware_family is None:
        found = _marlin_family(report)
        if found:
            settle("firmware_family", *found)
    if facts.z_home_method is None:
        found_method = _marlin_z_home_method(report, facts)
        if found_method:
            settle("z_home_method", *found_method)
    # The one place the unit's word outranks the vendor's: soft endstops are
    # a runtime switch the owner can flip and the firmware remembers, and a
    # unit reporting them OFF will not clamp an unhomed move whatever the
    # vendor compiled in.  The safer reading wins; a refusal compiled in
    # underneath (NO_MOTION_BEFORE_HOMING) only makes the caveat cautious.
    if getattr(report, "soft_endstops_on", None) is False and facts.unhomed_move_policy in (None, "clamped"):
        was = f"the vendor's record said {facts.unhomed_move_policy}; " if facts.unhomed_move_policy else ""
        settle("unhomed_move_policy", "unclamped",
               was + "M211 reports Soft endstops: Off on this unit, so nothing clamps an unhomed move "
               "(a compiled-in refusal, if any, is not reported)")
    if not updates:
        return facts
    return replace(facts, sources=sources, **updates)


def fill_from_machine(facts: MotionFacts, kind: str, payload: Any) -> MotionFacts:
    """Dispatch on what the machine handed over: a Klipper config or Marlin reports."""
    if kind == "klipper_config":
        return fill_from_klipper_config(facts, payload)
    if kind == "marlin_report":
        return fill_from_marlin_report(facts, payload)
    return facts
