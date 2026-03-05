"""G-code safety validator for the Kiln project.

Validates G-code commands before they are sent to a printer, catching
dangerous operations that could cause physical damage, overheating, or
fire risk.  This is critical safety infrastructure -- AI agents sending
raw G-code must pass through this validator first.

Safety categories
-----------------
**BLOCKING** errors prevent the command from being sent at all:
    - Temperature values above safe maximums
    - Commands that modify firmware settings, network config, or trigger
      emergency stop (which should go through dedicated tools instead)

**WARNING** issues are logged but do not prevent the command:
    - Movement below the bed plane (Z < 0)
    - Unusually high feedrates
    - Disabling stepper motors (risk of part shifting)
    - Homing commands (allowed, but caller should be aware)

Usage::

    from kiln.gcode import validate_gcode

    result = validate_gcode("G28\\nM104 S200\\nG1 X10 Y10 Z0.2 F1200")
    if not result.valid:
        print("Blocked:", result.errors)
    else:
        for cmd in result.commands:
            send_to_printer(cmd)
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GCodeValidationResult:
    """Result of validating one or more G-code commands.

    Attributes:
        valid: ``True`` if every command passed validation (no blocking
            errors).  Commands that only generated warnings are still
            considered valid.
        commands: The parsed, cleaned list of commands that passed
            validation and are safe to send.
        warnings: Human-readable descriptions of non-blocking issues
            detected during validation.
        errors: Human-readable descriptions of blocking issues that
            caused one or more commands to be rejected.
        blocked_commands: The raw command strings that were rejected.
    """

    valid: bool = True
    commands: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked_commands: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# G-code dialect awareness
# ---------------------------------------------------------------------------


class GCodeDialect(Enum):
    """Firmware dialect for dialect-specific command validation."""

    GENERIC = "generic"
    MARLIN = "marlin"
    KLIPPER = "klipper"
    BAMBU = "bambu"


# Additional commands blocked per dialect.  GENERIC and MARLIN have no
# extra blocks -- they use only the global ``_BLOCKED_COMMANDS`` table.
_DIALECT_BLOCKED: dict[GCodeDialect, dict[str, str]] = {
    GCodeDialect.GENERIC: {},
    GCodeDialect.MARLIN: {},
    GCodeDialect.KLIPPER: {
        "M500": "M500 is not supported on Klipper -- use SAVE_CONFIG instead",
        "M501": "M501 is not supported on Klipper -- use SAVE_CONFIG instead",
        "M502": "M502 is not supported on Klipper -- use SAVE_CONFIG instead",
    },
    GCodeDialect.BAMBU: {
        "M600": "M600 (filament change) is not supported on Bambu Lab printers during a print",
    },
}


# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------

# Maximum temperature values in degrees Celsius.
_MAX_HOTEND_TEMP: float = 300.0
_MAX_BED_TEMP: float = 130.0
_MAX_CHAMBER_TEMP: float = 80.0

# Minimum temperature for cold-extrusion protection.  Setting a hotend
# below this value while extruding can jam the nozzle.
_MIN_EXTRUDE_TEMP: float = 150.0

# Movement safety thresholds.
_MIN_SAFE_Z: float = 0.0
_MAX_SAFE_FEEDRATE: float = 10_000.0

# Hotend temperature commands: M104 (set, no wait), M109 (set and wait).
_HOTEND_TEMP_COMMANDS: set[str] = {"M104", "M109"}

# Bed temperature commands: M140 (set, no wait), M190 (set and wait).
_BED_TEMP_COMMANDS: set[str] = {"M140", "M190"}

# Chamber temperature command.
_CHAMBER_TEMP_COMMANDS: set[str] = {"M141"}

# Commands that are unconditionally blocked.
_BLOCKED_COMMANDS: dict[str, str] = {
    "M112": "Emergency stop (M112) is blocked -- use the cancel_print tool instead",
    "M502": "Reset to factory defaults (M502) is blocked -- this can overwrite critical calibration",
    "M500": "Save settings to EEPROM (M500) is blocked -- agents must not persist firmware changes",
    "M501": "Load settings from EEPROM (M501) is blocked -- agents must not modify active firmware settings",
    "M552": "Network configuration (M552) is blocked -- agents must not modify network settings",
    "M553": "Network configuration (M553) is blocked -- agents must not modify network settings",
    "M554": "Network configuration (M554) is blocked -- agents must not modify network settings",
    "M997": "Firmware update (M997) is blocked -- agents must not trigger firmware updates",
}

# Commands that generate warnings but are allowed.
_WARN_COMMANDS: dict[str, str] = {
    "G28": "G28 will home all axes -- ensure the bed is clear",
    "M18": "M18 will disable stepper motors -- part may shift if on the bed",
    "M84": "M84 will disable stepper motors -- part may shift if on the bed",
    "M906": "M906 modifies stepper motor current -- incorrect values can damage hardware",
}

# Movement commands that can carry Z and F parameters.
_MOVE_COMMANDS: set[str] = {"G0", "G1", "G2", "G3"}

# Arc commands that require I/J or R parameters.
_ARC_COMMANDS: set[str] = {"G2", "G3"}

# Material temperature ranges: (hotend_min, hotend_max, bed_min, bed_max)
_MATERIAL_TEMPS: dict[str, tuple[float, float, float, float]] = {
    "PLA": (180.0, 220.0, 40.0, 70.0),
    "PETG": (220.0, 260.0, 60.0, 90.0),
    "ABS": (230.0, 270.0, 90.0, 115.0),
    "TPU": (210.0, 240.0, 30.0, 60.0),
    "ASA": (230.0, 270.0, 90.0, 115.0),
    "Nylon": (240.0, 280.0, 60.0, 80.0),
    "PC": (260.0, 310.0, 90.0, 120.0),
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Regex to extract parameters from a G-code command.  Matches a letter
# followed by an optional sign and a number (integer or float).
_PARAM_RE = re.compile(r"([A-Za-z])\s*([+-]?\d*\.?\d+)")

# Regex to extract the command word (letter + digits) from the start of
# a stripped line.  Tolerates an optional space between letter and number.
_CMD_RE = re.compile(r"^([A-Za-z])\s*(\d+(?:\.\d+)?)")

# Regex to match a leading line-number word (e.g. ``N10``, ``N100``).
# The N-word is optional and is stripped before parsing the actual command.
_LINE_NUMBER_RE = re.compile(r"^[Nn]\d+\s*")


def _strip_line_number(line: str) -> str:
    """Remove a leading G-code line number (N-word) if present.

    G-code lines may be prefixed with ``N<digits>`` (with or without a
    trailing space) for transmission error-checking.  This prefix is not
    part of the command and must be removed before parsing.

    Examples::

        >>> _strip_line_number("N10 G28")
        'G28'
        >>> _strip_line_number("N100G28")
        'G28'
        >>> _strip_line_number("G28")
        'G28'
    """
    return _LINE_NUMBER_RE.sub("", line)


def _parse_command_word(line: str) -> str | None:
    """Extract and normalise the command word from a G-code line.

    Returns the command in upper-case canonical form (e.g. ``"G28"``,
    ``"M104"``), or ``None`` if the line doesn't start with a recognised
    command pattern.
    """
    m = _CMD_RE.match(line)
    if m is None:
        return None
    letter = m.group(1).upper()
    number = m.group(2)
    # Normalise: strip leading zeros, drop trailing ".0" for integer commands.
    try:
        num_val = float(number)
        if num_val == int(num_val):
            number = str(int(num_val))
        else:
            number = str(num_val)
    except ValueError:
        pass
    return f"{letter}{number}"


def _extract_param(line: str, letter: str) -> float | None:
    """Extract the numeric value for parameter *letter* from *line*.

    The search is case-insensitive.  Returns ``None`` if the parameter
    is not present.
    """
    upper = letter.upper()
    for m in _PARAM_RE.finditer(line):
        if m.group(1).upper() == upper:
            try:
                return float(m.group(2))
            except ValueError:
                return None
    return None


def _strip_comment(line: str) -> str:
    """Remove inline comments (everything from ``;`` onward) and strip whitespace."""
    idx = line.find(";")
    if idx != -1:
        line = line[:idx]
    return line.strip()


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------


def _validate_single(
    raw_line: str,
    warnings: list[str],
    errors: list[str],
    blocked: list[str],
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> str | None:
    """Validate a single G-code line.

    If the line is safe, returns the cleaned command string.  If the line
    is blocked, appends to *errors* and *blocked* and returns ``None``.
    Non-blocking issues are appended to *warnings*.

    When *dialect* is not ``GENERIC``, dialect-specific blocked commands
    are also checked.
    """
    cleaned = _strip_comment(raw_line)
    if not cleaned:
        return None  # blank / comment-only line -- skip silently
    cleaned = _strip_line_number(cleaned)

    cmd = _parse_command_word(cleaned)
    if cmd is None:
        # Not a recognisable G-code command -- block to prevent sending
        # arbitrary text to the printer firmware.
        errors.append(f"Unrecognised command format blocked: {cleaned!r}")
        blocked.append(cleaned)
        return None

    # --- Blocked commands ------------------------------------------------
    if cmd in _BLOCKED_COMMANDS:
        errors.append(_BLOCKED_COMMANDS[cmd])
        blocked.append(cleaned)
        return None

    # --- Dialect-specific blocked commands --------------------------------
    dialect_blocks = _DIALECT_BLOCKED.get(dialect, {})
    if cmd in dialect_blocks:
        errors.append(dialect_blocks[cmd])
        blocked.append(cleaned)
        return None

    # --- Warning-level commands ------------------------------------------
    if cmd in _WARN_COMMANDS:
        warnings.append(_WARN_COMMANDS[cmd])

    # --- Temperature limits (BLOCKING) -----------------------------------
    if cmd in _HOTEND_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            if temp > _MAX_HOTEND_TEMP:
                errors.append(f"{cmd} S{temp:g} exceeds maximum hotend temperature ({_MAX_HOTEND_TEMP:g}C)")
                blocked.append(cleaned)
                return None
            if 0 < temp < _MIN_EXTRUDE_TEMP:
                warnings.append(
                    f"{cmd} S{temp:g} is below minimum extrusion temperature "
                    f"({_MIN_EXTRUDE_TEMP:g}C) -- risk of cold extrusion jam"
                )

    if cmd in _BED_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            if temp > _MAX_BED_TEMP:
                errors.append(f"{cmd} S{temp:g} exceeds maximum bed temperature ({_MAX_BED_TEMP:g}C)")
                blocked.append(cleaned)
                return None

    if cmd in _CHAMBER_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            if temp > _MAX_CHAMBER_TEMP:
                errors.append(f"{cmd} S{temp:g} exceeds maximum chamber temperature ({_MAX_CHAMBER_TEMP:g}C)")
                blocked.append(cleaned)
                return None

    # --- Movement safety (WARNING) ---------------------------------------
    if cmd in _MOVE_COMMANDS:
        z_val = _extract_param(cleaned, "Z")
        if z_val is not None and z_val < _MIN_SAFE_Z:
            warnings.append(f"{cmd} moves Z to {z_val:g} which is below the bed plane (Z < 0)")

        f_val = _extract_param(cleaned, "F")
        if f_val is not None and f_val > _MAX_SAFE_FEEDRATE:
            warnings.append(f"{cmd} feedrate F{f_val:g} exceeds recommended maximum ({_MAX_SAFE_FEEDRATE:g} mm/min)")

    # --- Arc command validation (G2/G3) ----------------------------------
    if cmd in _ARC_COMMANDS:
        i_val = _extract_param(cleaned, "I")
        j_val = _extract_param(cleaned, "J")
        r_val = _extract_param(cleaned, "R")
        has_ij = i_val is not None or j_val is not None
        has_r = r_val is not None
        if not has_ij and not has_r:
            warnings.append(f"{cmd} arc command missing I/J or R parameters \u2014 arc is undefined")
        if has_r and r_val == 0:
            warnings.append(f"{cmd} has R=0 (zero-radius arc is degenerate)")

    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_gcode(
    commands: str | list[str],
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> GCodeValidationResult:
    """Parse and validate G-code commands for safety.

    Accepts either a single string (with commands separated by newlines)
    or a list of individual command strings.  Each command is cleaned
    (comments stripped, whitespace normalised) and checked against the
    safety rules.

    Args:
        commands: One or more G-code commands.  A string is split on
            newlines; a list is processed element-by-element.
        dialect: Firmware dialect for dialect-specific validation.

    Returns:
        A :class:`GCodeValidationResult` summarising which commands are
        safe to send, which were blocked, and any warnings.

    Examples:
        >>> r = validate_gcode("G28")
        >>> r.valid
        True
        >>> r.commands
        ['G28']

        >>> r = validate_gcode("M104 S999")
        >>> r.valid
        False
        >>> len(r.errors)
        1
    """
    if isinstance(commands, str):
        lines = commands.splitlines()
    else:
        # Flatten: each element might itself contain newlines.
        lines = []
        for item in commands:
            lines.extend(item.splitlines())

    result = GCodeValidationResult()

    for raw_line in lines:
        cleaned = _validate_single(
            raw_line,
            result.warnings,
            result.errors,
            result.blocked_commands,
            dialect=dialect,
        )
        if cleaned is not None:
            result.commands.append(cleaned)

    result.valid = len(result.errors) == 0
    return result


def validate_gcode_for_printer(
    commands: str | list[str],
    printer_id: str,
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> GCodeValidationResult:
    """Validate G-code using printer-specific safety limits.

    Loads the safety profile for *printer_id* from the bundled database
    and applies its temperature/feedrate limits instead of the generic
    defaults.  Falls back to ``validate_gcode()`` if no profile is found.

    Args:
        commands: One or more G-code commands.
        printer_id: Printer profile identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``).
        dialect: Firmware dialect for dialect-specific validation.

    Returns:
        A :class:`GCodeValidationResult`.
    """
    # Lazy import to avoid circular dependency and keep base validator
    # independent of the data layer.
    from kiln.safety_profiles import get_profile  # noqa: E402

    try:
        profile = get_profile(printer_id)
    except KeyError:
        return validate_gcode(commands, dialect=dialect)

    if isinstance(commands, str):
        lines = commands.splitlines()
    else:
        lines = []
        for item in commands:
            lines.extend(item.splitlines())

    result = GCodeValidationResult()

    for raw_line in lines:
        cleaned = _validate_single_with_profile(
            raw_line,
            result.warnings,
            result.errors,
            result.blocked_commands,
            profile,
            dialect=dialect,
        )
        if cleaned is not None:
            result.commands.append(cleaned)

    result.valid = len(result.errors) == 0
    return result


def validate_gcode_for_material(
    commands: str | list[str],
    material: str,
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> GCodeValidationResult:
    """Validate G-code temperatures against a material's expected range.

    Runs standard validation first, then adds warnings for any
    temperature commands that fall outside the expected range for the
    specified material.  A bed temperature of 0 with a material that
    requires a heated bed generates a warning.

    :param commands: One or more G-code commands.
    :param material: Material name (e.g. ``"PLA"``, ``"ABS"``).
    :param dialect: Firmware dialect for dialect-specific validation.
    """
    result = validate_gcode(commands, dialect=dialect)

    mat_range = _MATERIAL_TEMPS.get(material)
    if mat_range is None:
        result.warnings.append(f"Unknown material {material!r} -- skipping temperature validation.")
        return result

    hotend_min, hotend_max, bed_min, bed_max = mat_range

    # Re-parse to check temperatures against material ranges
    if isinstance(commands, str):
        lines = commands.splitlines()
    else:
        lines = list(commands)

    for line in lines:
        cleaned = line.split(";")[0].strip().upper()
        if not cleaned:
            continue

        parts = cleaned.split()
        if not parts:
            continue
        cmd = parts[0]

        # Parse S parameter (temperature setpoint)
        s_val: float | None = None
        for part in parts[1:]:
            if part.startswith("S"):
                with contextlib.suppress(ValueError):
                    s_val = float(part[1:])

        if s_val is None:
            continue

        # Check hotend temperature commands
        if cmd in _HOTEND_TEMP_COMMANDS:
            if s_val > 0 and s_val < hotend_min:
                result.warnings.append(
                    f"{cmd} S{s_val:.0f} is below {material} minimum hotend temp ({hotend_min:.0f}\u00b0C)."
                )
            elif s_val > hotend_max:
                result.warnings.append(
                    f"{cmd} S{s_val:.0f} exceeds {material} maximum hotend temp ({hotend_max:.0f}\u00b0C)."
                )

        # Check bed temperature commands
        if cmd in _BED_TEMP_COMMANDS:
            if s_val == 0 and bed_min > 0:
                result.warnings.append(
                    f"{cmd} S0 sets bed to 0\u00b0C but {material} requires {bed_min:.0f}-{bed_max:.0f}\u00b0C. "
                    f"Print will likely fail due to adhesion."
                )
            elif s_val > 0 and s_val < bed_min:
                result.warnings.append(
                    f"{cmd} S{s_val:.0f} is below {material} minimum bed temp ({bed_min:.0f}\u00b0C)."
                )
            elif s_val > bed_max:
                result.warnings.append(
                    f"{cmd} S{s_val:.0f} exceeds {material} maximum bed temp ({bed_max:.0f}\u00b0C)."
                )

    return result


def _validate_single_with_profile(
    raw_line: str,
    warnings: list[str],
    errors: list[str],
    blocked: list[str],
    profile: Any,
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> str | None:
    """Validate a single G-code line against a printer safety profile.

    Works like ``_validate_single`` but uses limits from *profile*
    instead of module-level constants.

    When *dialect* is not ``GENERIC``, dialect-specific blocked commands
    are also checked.
    """
    cleaned = _strip_comment(raw_line)
    if not cleaned:
        return None
    cleaned = _strip_line_number(cleaned)

    cmd = _parse_command_word(cleaned)
    if cmd is None:
        errors.append(f"Unrecognised command format blocked: {cleaned!r}")
        blocked.append(cleaned)
        return None

    # --- Blocked commands (same regardless of printer) ---
    if cmd in _BLOCKED_COMMANDS:
        errors.append(_BLOCKED_COMMANDS[cmd])
        blocked.append(cleaned)
        return None

    # --- Dialect-specific blocked commands ---
    dialect_blocks = _DIALECT_BLOCKED.get(dialect, {})
    if cmd in dialect_blocks:
        errors.append(dialect_blocks[cmd])
        blocked.append(cleaned)
        return None

    # --- Warning-level commands ---
    if cmd in _WARN_COMMANDS:
        warnings.append(_WARN_COMMANDS[cmd])

    # --- Temperature limits from profile (BLOCKING) ---
    if cmd in _HOTEND_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_hotend_temp
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            if temp > limit:
                errors.append(f"{cmd} S{temp:g} exceeds {profile.display_name} max hotend temperature ({limit:g}°C)")
                blocked.append(cleaned)
                return None
            if 0 < temp < _MIN_EXTRUDE_TEMP:
                warnings.append(
                    f"{cmd} S{temp:g} is below minimum extrusion temperature "
                    f"({_MIN_EXTRUDE_TEMP:g}C) -- risk of cold extrusion jam"
                )

    if cmd in _BED_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_bed_temp
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            if temp > limit:
                errors.append(f"{cmd} S{temp:g} exceeds {profile.display_name} max bed temperature ({limit:g}°C)")
                blocked.append(cleaned)
                return None

    if cmd in _CHAMBER_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_chamber_temp
        if temp is not None:
            if temp < 0:
                errors.append(f"{cmd} S{temp:g} has negative temperature -- temperatures must be >= 0")
                blocked.append(cleaned)
                return None
            effective_limit = limit if limit is not None else _MAX_CHAMBER_TEMP
            if temp > effective_limit:
                errors.append(f"{cmd} S{temp:g} exceeds {profile.display_name} max chamber temperature ({effective_limit:g}°C)")
                blocked.append(cleaned)
                return None

    # --- Movement safety from profile (WARNING) ---
    if cmd in _MOVE_COMMANDS:
        z_val = _extract_param(cleaned, "Z")
        if z_val is not None and z_val < profile.min_safe_z:
            warnings.append(f"{cmd} moves Z to {z_val:g} which is below the bed plane (Z < {profile.min_safe_z:g})")

        f_val = _extract_param(cleaned, "F")
        if f_val is not None and f_val > profile.max_feedrate:
            warnings.append(
                f"{cmd} feedrate F{f_val:g} exceeds {profile.display_name} "
                f"recommended maximum ({profile.max_feedrate:g} mm/min)"
            )

        # --- Volumetric flow estimation (WARNING) ---
        # For extrusion moves (G1 with E parameter), estimate volumetric flow
        # from feedrate and extrusion ratio.  Flow = (E / distance) * feedrate
        # as a rough proxy for mm³/s.  Only warn when a profile limit exists.
        if cmd == "G1" and profile.max_volumetric_flow is not None:
            e_val = _extract_param(cleaned, "E")
            if e_val is not None and e_val > 0 and f_val is not None and f_val > 0:
                # Estimate distance from XY movement (fall back to feedrate-based)
                x_val = _extract_param(cleaned, "X")
                y_val = _extract_param(cleaned, "Y")
                dist = 0.0
                if x_val is not None or y_val is not None:
                    dx = x_val if x_val is not None else 0.0
                    dy = y_val if y_val is not None else 0.0
                    dist = (dx**2 + dy**2) ** 0.5
                if dist > 0:
                    # Filament cross-section: 1.75mm diameter -> area ~2.405 mm²
                    filament_area = 2.405
                    # Volumetric flow = filament_area * (E/dist) * (feedrate / 60)
                    flow = filament_area * (e_val / dist) * (f_val / 60.0)
                    if flow > profile.max_volumetric_flow:
                        warnings.append(
                            f"{cmd} estimated volumetric flow {flow:.1f} mm³/s exceeds "
                            f"{profile.display_name} maximum ({profile.max_volumetric_flow:g} mm³/s)"
                        )

        # --- Build volume validation (WARNING) ---
        # Check XY/Z coordinates against the printer's build volume when known.
        if profile.build_volume is not None and len(profile.build_volume) >= 3:
            bv_x, bv_y, bv_z = profile.build_volume[0], profile.build_volume[1], profile.build_volume[2]
            x_val = _extract_param(cleaned, "X")
            y_val = _extract_param(cleaned, "Y")
            if x_val is not None and (x_val < 0 or x_val > bv_x):
                warnings.append(f"{cmd} X{x_val:g} is outside {profile.display_name} build volume (X: 0–{bv_x} mm)")
            if y_val is not None and (y_val < 0 or y_val > bv_y):
                warnings.append(f"{cmd} Y{y_val:g} is outside {profile.display_name} build volume (Y: 0–{bv_y} mm)")
            if z_val is not None and z_val > bv_z:
                warnings.append(f"{cmd} Z{z_val:g} is outside {profile.display_name} build volume (Z: 0–{bv_z} mm)")

    # --- Arc command validation (G2/G3) ---
    if cmd in _ARC_COMMANDS:
        i_val = _extract_param(cleaned, "I")
        j_val = _extract_param(cleaned, "J")
        r_val = _extract_param(cleaned, "R")
        has_ij = i_val is not None or j_val is not None
        has_r = r_val is not None
        if not has_ij and not has_r:
            warnings.append(f"{cmd} arc command missing I/J or R parameters \u2014 arc is undefined")
        if has_r and r_val == 0:
            warnings.append(f"{cmd} has R=0 (zero-radius arc is degenerate)")

    return cleaned


# ---------------------------------------------------------------------------
# Missing temperature detection
# ---------------------------------------------------------------------------

# All temperature commands that set hotend or bed temp.
_ALL_HOTEND_TEMP_COMMANDS: set[str] = _HOTEND_TEMP_COMMANDS  # M104, M109
_ALL_BED_TEMP_COMMANDS: set[str] = _BED_TEMP_COMMANDS  # M140, M190


def _check_missing_temperatures(result: GCodeValidationResult) -> None:
    """Check validated commands for missing temperature commands.

    Appends warnings to *result* if no hotend or bed temperature commands
    were found among the validated commands.  This catches gcode files
    that rely on the printer's current temperature state instead of
    explicitly setting temps -- a common source of failed prints.

    Only meaningful for files with actual movement/extrusion commands.
    """
    has_hotend = False
    has_bed = False
    has_extrusion = False

    for cmd_line in result.commands:
        cmd_word = _parse_command_word(cmd_line)
        if cmd_word is None:
            continue

        if cmd_word in _ALL_HOTEND_TEMP_COMMANDS:
            temp = _extract_param(cmd_line, "S")
            if temp is not None and temp > 0:
                has_hotend = True
        elif cmd_word in _ALL_BED_TEMP_COMMANDS:
            temp = _extract_param(cmd_line, "S")
            if temp is not None and temp > 0:
                has_bed = True
        elif cmd_word == "G1":
            # Check for extrusion moves (E parameter present)
            e_val = _extract_param(cmd_line, "E")
            if e_val is not None and e_val > 0:
                has_extrusion = True

    # Only warn if the file actually has extrusion commands -- pure
    # movement/homing scripts shouldn't need temperature commands.
    if not has_extrusion:
        return

    if not has_hotend:
        result.warnings.append(
            "No hotend temperature command found (M104/M109). "
            "The printer will use whatever temperature is currently set, "
            "which may cause cold extrusion or print failure."
        )
    if not has_bed:
        result.warnings.append(
            "No bed temperature command found (M140/M190). "
            "The printer will use whatever bed temperature is currently set, "
            "which may cause adhesion failure."
        )


def check_missing_temperatures(
    commands: str | list[str],
) -> list[str]:
    """Check G-code for missing temperature commands.

    Scans the commands for hotend (M104/M109) and bed (M140/M190)
    temperature setpoints.  Returns a list of warning strings for any
    missing categories.  Returns an empty list if both are present or
    if the gcode has no extrusion commands.

    This is a lightweight check suitable for preflight validation --
    it does NOT perform full G-code safety validation.

    Args:
        commands: G-code as a string (newline-separated) or list of lines.

    Returns:
        List of warning strings.  Empty if no issues found.
    """
    # Build a minimal GCodeValidationResult with parsed command words
    if isinstance(commands, str):
        lines = commands.splitlines()
    else:
        lines = []
        for item in commands:
            lines.extend(item.splitlines())

    # Clean lines (strip comments, blank lines)
    cleaned: list[str] = []
    for raw in lines:
        line = _strip_comment(raw)
        if line:
            line = _strip_line_number(line)
            if _parse_command_word(line) is not None:
                cleaned.append(line)

    result = GCodeValidationResult(commands=cleaned)
    _check_missing_temperatures(result)
    return result.warnings


# ---------------------------------------------------------------------------
# Bambu-specific G-code validation
# ---------------------------------------------------------------------------

# Reasonable temperature ranges for Bambu printers by material category.
_BAMBU_NOZZLE_TEMP_RANGE: tuple[float, float] = (150.0, 300.0)
_BAMBU_BED_TEMP_RANGE: tuple[float, float] = (35.0, 120.0)


@dataclass
class BambuGcodeValidation:
    """Result of Bambu-specific G-code preflight validation.

    :param valid: ``True`` if the gcode is safe to send to a Bambu printer.
    :param errors: Blocking issues that will cause print failure.
    :param warnings: Non-blocking concerns.
    :param has_nozzle_temp: Whether any M104/M109 command was found.
    :param has_bed_temp: Whether any M140/M190 command was found.
    :param nozzle_temp: The highest nozzle temperature setpoint found.
    :param bed_temp: The highest bed temperature setpoint found.
    """

    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_nozzle_temp: bool = False
    has_bed_temp: bool = False
    nozzle_temp: float | None = None
    bed_temp: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "has_nozzle_temp": self.has_nozzle_temp,
            "has_bed_temp": self.has_bed_temp,
            "nozzle_temp": self.nozzle_temp,
            "bed_temp": self.bed_temp,
        }


def validate_bambu_gcode(
    gcode: str,
) -> BambuGcodeValidation:
    """Validate G-code for Bambu-specific requirements before upload.

    Checks that the gcode contains required temperature commands and that
    temperature values are within reasonable ranges.  This catches the
    common failure where gcode has been stripped of temperature commands
    (M104/M109/M140/M190), causing the printer to enter PREPARE state,
    move around (homing, bed leveling), but never heat the nozzle.

    Args:
        gcode: The full G-code content as a string.

    Returns:
        A :class:`BambuGcodeValidation` with errors/warnings.
    """
    result = BambuGcodeValidation()
    has_extrusion = False
    max_nozzle: float = 0.0
    max_bed: float = 0.0

    for raw_line in gcode.splitlines():
        line = raw_line.split(";")[0].strip()
        if not line:
            continue

        cmd_word = _parse_command_word(line)
        if cmd_word is None:
            continue

        # Check for temperature commands.
        if cmd_word in _HOTEND_TEMP_COMMANDS:
            temp = _extract_param(line, "S")
            if temp is not None and temp > 0:
                result.has_nozzle_temp = True
                if temp > max_nozzle:
                    max_nozzle = temp

        elif cmd_word in _BED_TEMP_COMMANDS:
            temp = _extract_param(line, "S")
            if temp is not None and temp > 0:
                result.has_bed_temp = True
                if temp > max_bed:
                    max_bed = temp

        elif cmd_word == "G1":
            e_val = _extract_param(line, "E")
            if e_val is not None and e_val > 0:
                has_extrusion = True

    # Only validate if gcode has extrusion commands (not pure movement scripts).
    if not has_extrusion:
        return result

    # Missing temperature commands → blocking errors.
    if not result.has_nozzle_temp:
        result.errors.append(
            "No nozzle temperature command found (M104/M109). "
            "The printer will enter PREPARE state but never heat the nozzle, "
            "causing the print to hang indefinitely. Add M104/M109 with a "
            "temperature appropriate for your filament."
        )

    if not result.has_bed_temp:
        result.errors.append(
            "No bed temperature command found (M140/M190). "
            "The printer may fail to achieve proper adhesion. "
            "Add M140/M190 with a temperature appropriate for your filament."
        )

    # Temperature range validation.
    if max_nozzle > 0:
        result.nozzle_temp = max_nozzle
        lo, hi = _BAMBU_NOZZLE_TEMP_RANGE
        if max_nozzle < lo:
            result.warnings.append(
                f"Nozzle temperature {max_nozzle:.0f}°C is below "
                f"minimum recommended ({lo:.0f}°C) — risk of cold extrusion."
            )
        elif max_nozzle > hi:
            result.errors.append(
                f"Nozzle temperature {max_nozzle:.0f}°C exceeds "
                f"Bambu maximum ({hi:.0f}°C)."
            )

    if max_bed > 0:
        result.bed_temp = max_bed
        lo, hi = _BAMBU_BED_TEMP_RANGE
        if max_bed < lo:
            result.warnings.append(
                f"Bed temperature {max_bed:.0f}°C is below "
                f"minimum recommended ({lo:.0f}°C)."
            )
        elif max_bed > hi:
            result.errors.append(
                f"Bed temperature {max_bed:.0f}°C exceeds "
                f"Bambu maximum ({hi:.0f}°C)."
            )

    result.valid = len(result.errors) == 0
    return result


# ---------------------------------------------------------------------------
# File-level scanning
# ---------------------------------------------------------------------------

_MAX_SCAN_BYTES: int = 500 * 1024 * 1024  # 500 MB text cap
_MAX_WARNINGS: int = 50
_MAX_HEADER_LINES: int = 100  # Lines to scan for printer model detection


# Regex patterns for slicer-embedded printer model comments.
# Each pattern captures the printer name/model string.
_PRINTER_HEADER_PATTERNS: list[re.Pattern] = [
    # PrusaSlicer / OrcaSlicer / BambuStudio
    re.compile(r";\s*printer_model\s*=\s*(.+)", re.IGNORECASE),
    re.compile(r";\s*printer_settings_id\s*[:=]\s*(.+)", re.IGNORECASE),
    re.compile(r";\s*machine_name\s*=\s*(.+)", re.IGNORECASE),
    # Cura
    re.compile(r";\s*MACHINE_TYPE\s*[:=]\s*(.+)", re.IGNORECASE),
    # Generic
    re.compile(r";\s*Target\s*[:=]\s*(.+)", re.IGNORECASE),
    re.compile(r";\s*printer\s*[:=]\s*(.+)", re.IGNORECASE),
]


def detect_printer_from_header(file_path: str) -> str | None:
    """Try to detect the printer model from G-code file header comments.

    Reads the first :data:`_MAX_HEADER_LINES` lines of the file looking for
    slicer-embedded printer model comments (PrusaSlicer, Cura, OrcaSlicer,
    BambuStudio all embed this information in G-code file comments).

    Returns a matched safety profile ID if found, or ``None`` if no match.
    """
    try:
        with open(file_path, errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _MAX_HEADER_LINES:
                    break
                for pattern in _PRINTER_HEADER_PATTERNS:
                    m = pattern.match(line.strip())
                    if m:
                        raw_name = m.group(1).strip()
                        if raw_name:
                            matched = _match_printer_name(raw_name)
                            if matched:
                                return matched
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def _match_printer_name(raw_name: str) -> str | None:
    """Fuzzy-match a slicer-embedded printer name to a safety profile ID.

    Normalises the name (lowercase, strip common prefixes and separators)
    and attempts to match against the bundled safety profiles.
    """
    try:
        from kiln.safety_profiles import get_profile, list_profiles  # noqa: E402

        # Direct match attempt via get_profile (which does its own normalisation)
        try:
            profile = get_profile(raw_name)
            if profile.id != "default":
                return profile.id
        except KeyError:
            pass

        # Normalise: lowercase, strip common prefixes, replace separators
        normalised = raw_name.lower()
        for prefix in ("prusa ", "creality ", "bambu lab ", "bambulab ", "anycubic "):
            if normalised.startswith(prefix):
                normalised = normalised[len(prefix) :]
        normalised = normalised.replace("-", "_").replace(" ", "_").strip("_")

        # Try again after normalisation
        try:
            profile = get_profile(normalised)
            if profile.id != "default":
                return profile.id
        except KeyError:
            pass

        # Substring matching: check if any profile ID appears in the name or vice versa
        all_ids = list_profiles()
        for pid in all_ids:
            if pid == "default":
                continue
            if pid in normalised or normalised in pid:
                return pid

    except ImportError:
        pass
    return None


def scan_gcode_file(
    file_path: str,
    *,
    printer_id: str | None = None,
    dialect: GCodeDialect = GCodeDialect.GENERIC,
) -> GCodeValidationResult:
    """Stream-validate an entire G-code file for safety.

    Reads the file line-by-line without loading it into memory.  Checks
    every line against blocked commands and temperature limits.  Fails fast
    on the first **blocked** command to avoid reading unnecessarily large
    files.  Warnings are collected but capped at :data:`_MAX_WARNINGS`.

    When *printer_id* is provided the per-printer safety profile is used
    for temperature and feedrate limits.  Otherwise the generic module-level
    limits apply.

    When *dialect* is not ``GENERIC``, dialect-specific blocked commands
    are also checked.

    Args:
        file_path: Path to a ``.gcode`` / ``.gco`` / ``.g`` file.
        printer_id: Optional printer profile id (e.g. ``"ender3"``).
        dialect: Firmware dialect for dialect-specific validation.

    Returns:
        A :class:`GCodeValidationResult`.  ``valid`` is ``False`` if any
        blocked command was found.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        PermissionError: If the file cannot be read.
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"File not found: {abs_path}")

    file_size = os.path.getsize(abs_path)
    if file_size > _MAX_SCAN_BYTES:
        result = GCodeValidationResult(valid=False)
        result.errors.append(
            f"File is too large to scan ({file_size / 1024 / 1024:.1f} MB). "
            f"Maximum scannable size is {_MAX_SCAN_BYTES / 1024 / 1024:.0f} MB."
        )
        return result

    # Resolve validation function based on printer profile.
    # Auto-detect from file header if no explicit printer_id was provided.
    effective_id = printer_id
    if not effective_id:
        effective_id = detect_printer_from_header(abs_path)

    profile = None
    if effective_id:
        try:
            from kiln.safety_profiles import get_profile  # noqa: E402

            profile = get_profile(effective_id)
        except KeyError:
            pass  # fall back to generic validation

    result = GCodeValidationResult()

    with open(abs_path, errors="replace") as fh:
        for raw_line in fh:
            if profile is not None:
                cleaned = _validate_single_with_profile(
                    raw_line,
                    result.warnings,
                    result.errors,
                    result.blocked_commands,
                    profile,
                    dialect=dialect,
                )
            else:
                cleaned = _validate_single(
                    raw_line,
                    result.warnings,
                    result.errors,
                    result.blocked_commands,
                    dialect=dialect,
                )

            if cleaned is not None:
                result.commands.append(cleaned)

            # Fail fast on blocked commands.
            if result.errors:
                result.valid = False
                return result

            # Cap warnings to avoid unbounded memory growth.
            if len(result.warnings) > _MAX_WARNINGS:
                result.warnings = result.warnings[:_MAX_WARNINGS]
                result.warnings.append(f"(warnings capped at {_MAX_WARNINGS} -- additional warnings suppressed)")
                break

    result.valid = len(result.errors) == 0

    # -- Missing temperature check (warning, not blocking) -----------------
    if result.valid and result.commands:
        _check_missing_temperatures(result)

    return result
