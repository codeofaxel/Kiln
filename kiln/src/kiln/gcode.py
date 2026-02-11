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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union


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
# Safety limits
# ---------------------------------------------------------------------------

# Maximum temperature values in degrees Celsius.
_MAX_HOTEND_TEMP: float = 300.0
_MAX_BED_TEMP: float = 130.0
_MAX_CHAMBER_TEMP: float = 80.0

# Movement safety thresholds.
_MIN_SAFE_Z: float = 0.0
_MAX_SAFE_FEEDRATE: float = 10_000.0

# Hotend temperature commands: M104 (set, no wait), M109 (set and wait).
_HOTEND_TEMP_COMMANDS: Set[str] = {"M104", "M109"}

# Bed temperature commands: M140 (set, no wait), M190 (set and wait).
_BED_TEMP_COMMANDS: Set[str] = {"M140", "M190"}

# Chamber temperature command.
_CHAMBER_TEMP_COMMANDS: Set[str] = {"M141"}

# Commands that are unconditionally blocked.
_BLOCKED_COMMANDS: Dict[str, str] = {
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
_WARN_COMMANDS: Dict[str, str] = {
    "G28": "G28 will home all axes -- ensure the bed is clear",
    "M18": "M18 will disable stepper motors -- part may shift if on the bed",
    "M84": "M84 will disable stepper motors -- part may shift if on the bed",
    "M906": "M906 modifies stepper motor current -- incorrect values can damage hardware",
}

# Movement commands that can carry Z and F parameters.
_MOVE_COMMANDS: Set[str] = {"G0", "G1"}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Regex to extract parameters from a G-code command.  Matches a letter
# followed by an optional sign and a number (integer or float).
_PARAM_RE = re.compile(r"([A-Za-z])\s*([+-]?\d*\.?\d+)")

# Regex to extract the command word (letter + digits) from the start of
# a stripped line.  Tolerates an optional space between letter and number.
_CMD_RE = re.compile(r"^([A-Za-z])\s*(\d+(?:\.\d+)?)")


def _parse_command_word(line: str) -> Optional[str]:
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


def _extract_param(line: str, letter: str) -> Optional[float]:
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
    warnings: List[str],
    errors: List[str],
    blocked: List[str],
) -> Optional[str]:
    """Validate a single G-code line.

    If the line is safe, returns the cleaned command string.  If the line
    is blocked, appends to *errors* and *blocked* and returns ``None``.
    Non-blocking issues are appended to *warnings*.
    """
    cleaned = _strip_comment(raw_line)
    if not cleaned:
        return None  # blank / comment-only line -- skip silently

    cmd = _parse_command_word(cleaned)
    if cmd is None:
        # Not a recognisable G-code command -- pass through with a warning.
        warnings.append(f"Unrecognised command format: {cleaned!r}")
        return cleaned

    # --- Blocked commands ------------------------------------------------
    if cmd in _BLOCKED_COMMANDS:
        errors.append(_BLOCKED_COMMANDS[cmd])
        blocked.append(cleaned)
        return None

    # --- Warning-level commands ------------------------------------------
    if cmd in _WARN_COMMANDS:
        warnings.append(_WARN_COMMANDS[cmd])

    # --- Temperature limits (BLOCKING) -----------------------------------
    if cmd in _HOTEND_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None and temp > _MAX_HOTEND_TEMP:
            errors.append(
                f"{cmd} S{temp:g} exceeds maximum hotend temperature "
                f"({_MAX_HOTEND_TEMP:g}C)"
            )
            blocked.append(cleaned)
            return None

    if cmd in _BED_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None and temp > _MAX_BED_TEMP:
            errors.append(
                f"{cmd} S{temp:g} exceeds maximum bed temperature "
                f"({_MAX_BED_TEMP:g}C)"
            )
            blocked.append(cleaned)
            return None

    if cmd in _CHAMBER_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        if temp is not None and temp > _MAX_CHAMBER_TEMP:
            errors.append(
                f"{cmd} S{temp:g} exceeds maximum chamber temperature "
                f"({_MAX_CHAMBER_TEMP:g}C)"
            )
            blocked.append(cleaned)
            return None

    # --- Movement safety (WARNING) ---------------------------------------
    if cmd in _MOVE_COMMANDS:
        z_val = _extract_param(cleaned, "Z")
        if z_val is not None and z_val < _MIN_SAFE_Z:
            warnings.append(
                f"{cmd} moves Z to {z_val:g} which is below the bed plane (Z < 0)"
            )

        f_val = _extract_param(cleaned, "F")
        if f_val is not None and f_val > _MAX_SAFE_FEEDRATE:
            warnings.append(
                f"{cmd} feedrate F{f_val:g} exceeds recommended maximum "
                f"({_MAX_SAFE_FEEDRATE:g} mm/min)"
            )

    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_gcode(commands: Union[str, List[str]]) -> GCodeValidationResult:
    """Parse and validate G-code commands for safety.

    Accepts either a single string (with commands separated by newlines)
    or a list of individual command strings.  Each command is cleaned
    (comments stripped, whitespace normalised) and checked against the
    safety rules.

    Args:
        commands: One or more G-code commands.  A string is split on
            newlines; a list is processed element-by-element.

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
        )
        if cleaned is not None:
            result.commands.append(cleaned)

    result.valid = len(result.errors) == 0
    return result


def validate_gcode_for_printer(
    commands: Union[str, List[str]],
    printer_id: str,
) -> GCodeValidationResult:
    """Validate G-code using printer-specific safety limits.

    Loads the safety profile for *printer_id* from the bundled database
    and applies its temperature/feedrate limits instead of the generic
    defaults.  Falls back to ``validate_gcode()`` if no profile is found.

    Args:
        commands: One or more G-code commands.
        printer_id: Printer profile identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``).

    Returns:
        A :class:`GCodeValidationResult`.
    """
    # Lazy import to avoid circular dependency and keep base validator
    # independent of the data layer.
    from kiln.safety_profiles import get_profile  # noqa: E402

    try:
        profile = get_profile(printer_id)
    except KeyError:
        return validate_gcode(commands)

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
        )
        if cleaned is not None:
            result.commands.append(cleaned)

    result.valid = len(result.errors) == 0
    return result


def _validate_single_with_profile(
    raw_line: str,
    warnings: List[str],
    errors: List[str],
    blocked: List[str],
    profile: Any,
) -> Optional[str]:
    """Validate a single G-code line against a printer safety profile.

    Works like ``_validate_single`` but uses limits from *profile*
    instead of module-level constants.
    """
    cleaned = _strip_comment(raw_line)
    if not cleaned:
        return None

    cmd = _parse_command_word(cleaned)
    if cmd is None:
        warnings.append(f"Unrecognised command format: {cleaned!r}")
        return cleaned

    # --- Blocked commands (same regardless of printer) ---
    if cmd in _BLOCKED_COMMANDS:
        errors.append(_BLOCKED_COMMANDS[cmd])
        blocked.append(cleaned)
        return None

    # --- Warning-level commands ---
    if cmd in _WARN_COMMANDS:
        warnings.append(_WARN_COMMANDS[cmd])

    # --- Temperature limits from profile (BLOCKING) ---
    if cmd in _HOTEND_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_hotend_temp
        if temp is not None and temp > limit:
            errors.append(
                f"{cmd} S{temp:g} exceeds {profile.display_name} max hotend "
                f"temperature ({limit:g}°C)"
            )
            blocked.append(cleaned)
            return None

    if cmd in _BED_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_bed_temp
        if temp is not None and temp > limit:
            errors.append(
                f"{cmd} S{temp:g} exceeds {profile.display_name} max bed "
                f"temperature ({limit:g}°C)"
            )
            blocked.append(cleaned)
            return None

    if cmd in _CHAMBER_TEMP_COMMANDS:
        temp = _extract_param(cleaned, "S")
        limit = profile.max_chamber_temp
        if temp is not None and limit is not None and temp > limit:
            errors.append(
                f"{cmd} S{temp:g} exceeds {profile.display_name} max chamber "
                f"temperature ({limit:g}°C)"
            )
            blocked.append(cleaned)
            return None

    # --- Movement safety from profile (WARNING) ---
    if cmd in _MOVE_COMMANDS:
        z_val = _extract_param(cleaned, "Z")
        if z_val is not None and z_val < profile.min_safe_z:
            warnings.append(
                f"{cmd} moves Z to {z_val:g} which is below the bed plane "
                f"(Z < {profile.min_safe_z:g})"
            )

        f_val = _extract_param(cleaned, "F")
        if f_val is not None and f_val > profile.max_feedrate:
            warnings.append(
                f"{cmd} feedrate F{f_val:g} exceeds {profile.display_name} "
                f"recommended maximum ({profile.max_feedrate:g} mm/min)"
            )

    return cleaned
