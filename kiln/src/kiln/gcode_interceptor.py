"""Real-time G-code interception with AI override.

Middleware layer that intercepts G-code commands during active printing.
Each command is evaluated against live telemetry (temperature, position,
flow rate) and configurable rules before reaching the printer.  The
interceptor can BLOCK dangerous commands, MODIFY parameters (reduce speed,
cap temperature), PAUSE for human review, or ALERT the agent -- all in
real time.

This closes the gap between static pre-print validation
(:mod:`kiln.gcode`) and real-time adaptive control.

Usage::

    from kiln.gcode_interceptor import get_interceptor

    interceptor = get_interceptor()
    session = interceptor.create_session("ender3")
    result = interceptor.intercept(session.session_id, "M104 S280")
    # result.action == InterceptionAction.BLOCK
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_HISTORY_PER_SESSION: int = 500
_DEFAULT_TEMP_DELTA_THRESHOLD: float = 10.0  # C/sec thermal runaway indicator

# G-code parameter regex — matches letter + optional sign + number.
_PARAM_RE = re.compile(r"([A-Za-z])\s*([+-]?\d*\.?\d+)")

# Command word regex — letter + digits at start of line.
_CMD_RE = re.compile(r"^([A-Za-z])\s*(\d+(?:\.\d+)?)")

# Hotend temperature commands.
_HOTEND_TEMP_COMMANDS: frozenset[str] = frozenset({"M104", "M109"})

# Bed temperature commands.
_BED_TEMP_COMMANDS: frozenset[str] = frozenset({"M140", "M190"})

# Movement commands that carry feedrate and position.
_MOVE_COMMANDS: frozenset[str] = frozenset({"G0", "G1", "G2", "G3"})

# Commands that are always dangerous in real-time context.
_DEFAULT_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "M112",  # Emergency stop — use dedicated tool
        "M500",  # Save EEPROM
        "M501",  # Load EEPROM
        "M502",  # Factory reset
        "M552",  # Network config
        "M553",  # Network config
        "M554",  # Network config
        "M997",  # Firmware update
    }
)

# Layer-change detection patterns (common slicer comments).
_LAYER_CHANGE_RE = re.compile(
    r";\s*(?:LAYER_CHANGE|layer\s*:\s*\d+|Z:\s*[\d.]+|LAYER:\s*\d+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class InterceptionAction(str, Enum):
    """Action to take on an intercepted G-code command."""

    ALLOW = "allow"
    BLOCK = "block"
    MODIFY = "modify"
    PAUSE = "pause"
    ALERT = "alert"


class RulePriority(str, Enum):
    """Priority level for interception rules."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class InterceptionTrigger(str, Enum):
    """Condition that triggers a rule evaluation."""

    TEMP_EXCEEDS = "temp_exceeds"
    TEMP_BELOW = "temp_below"
    TEMP_DELTA = "temp_delta"
    FEEDRATE_EXCEEDS = "feedrate_exceeds"
    FLOW_ANOMALY = "flow_anomaly"
    POSITION_LIMIT = "position_limit"
    COMMAND_BLOCKED = "command_blocked"
    PATTERN_MATCH = "pattern_match"
    ALWAYS = "always"
    LAYER_CHANGE = "layer_change"


# Priority ordering for rule sorting (lower index = higher priority).
_PRIORITY_ORDER: dict[RulePriority, int] = {
    RulePriority.CRITICAL: 0,
    RulePriority.HIGH: 1,
    RulePriority.MEDIUM: 2,
    RulePriority.LOW: 3,
}

# Action precedence — higher-severity action wins when multiple rules fire.
_ACTION_PRECEDENCE: dict[InterceptionAction, int] = {
    InterceptionAction.BLOCK: 0,
    InterceptionAction.PAUSE: 1,
    InterceptionAction.MODIFY: 2,
    InterceptionAction.ALERT: 3,
    InterceptionAction.ALLOW: 4,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TelemetrySnapshot:
    """Current device state for rule evaluation."""

    hotend_temp: float | None = None
    hotend_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    position_x: float | None = None
    position_y: float | None = None
    position_z: float | None = None
    feedrate: float | None = None
    flow_rate_pct: float | None = None
    fan_speed_pct: float | None = None
    current_layer: int | None = None
    elapsed_seconds: float | None = None
    filament_used_mm: float | None = None
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "hotend_temp": self.hotend_temp,
            "hotend_target": self.hotend_target,
            "bed_temp": self.bed_temp,
            "bed_target": self.bed_target,
            "position_x": self.position_x,
            "position_y": self.position_y,
            "position_z": self.position_z,
            "feedrate": self.feedrate,
            "flow_rate_pct": self.flow_rate_pct,
            "fan_speed_pct": self.fan_speed_pct,
            "current_layer": self.current_layer,
            "elapsed_seconds": self.elapsed_seconds,
            "filament_used_mm": self.filament_used_mm,
            "timestamp": self.timestamp,
        }


@dataclass
class InterceptionRule:
    """A rule for intercepting G-code commands."""

    rule_id: str
    name: str
    trigger: InterceptionTrigger
    action: InterceptionAction
    priority: RulePriority
    threshold: float | None = None
    threshold_max: float | None = None
    blocked_commands: list[str] | None = None
    pattern: str | None = None
    modify_params: dict[str, Any] | None = None
    message: str = ""
    enabled: bool = True
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "trigger": self.trigger.value,
            "action": self.action.value,
            "priority": self.priority.value,
            "threshold": self.threshold,
            "threshold_max": self.threshold_max,
            "blocked_commands": self.blocked_commands,
            "pattern": self.pattern,
            "modify_params": self.modify_params,
            "message": self.message,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }


@dataclass
class InterceptionResult:
    """Result of intercepting a single G-code command."""

    original_command: str
    action: InterceptionAction
    modified_command: str | None = None
    triggered_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "original_command": self.original_command,
            "action": self.action.value,
            "modified_command": self.modified_command,
            "triggered_rules": self.triggered_rules,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }


@dataclass
class InterceptionSession:
    """An active interception session for a printer."""

    session_id: str
    printer_name: str
    rules: list[InterceptionRule] = field(default_factory=list)
    active: bool = True
    started_at: str = ""
    commands_processed: int = 0
    commands_blocked: int = 0
    commands_modified: int = 0
    commands_paused: int = 0
    alerts_issued: int = 0
    last_telemetry: TelemetrySnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "session_id": self.session_id,
            "printer_name": self.printer_name,
            "rules": [r.to_dict() for r in self.rules],
            "active": self.active,
            "started_at": self.started_at,
            "commands_processed": self.commands_processed,
            "commands_blocked": self.commands_blocked,
            "commands_modified": self.commands_modified,
            "commands_paused": self.commands_paused,
            "alerts_issued": self.alerts_issued,
            "last_telemetry": self.last_telemetry.to_dict() if self.last_telemetry else None,
        }


# ---------------------------------------------------------------------------
# G-code parsing helpers
# ---------------------------------------------------------------------------


def _parse_command_word(line: str) -> str | None:
    """Extract and normalise the command word from a G-code line."""
    stripped = line.strip()
    if not stripped:
        return None
    m = _CMD_RE.match(stripped)
    if m is None:
        return None
    letter = m.group(1).upper()
    number = m.group(2)
    try:
        num_val = float(number)
        if num_val == int(num_val):
            number = str(int(num_val))
        else:
            number = str(num_val)
    except ValueError:
        pass
    return f"{letter}{number}"


def _parse_gcode_params(command: str) -> dict[str, float]:
    """Extract letter-value pairs from a G-code command.

    Returns a dict mapping uppercase parameter letters to their float
    values.  The command word itself (e.g. ``G1``) is excluded.
    """
    params: dict[str, float] = {}
    cmd_word = _parse_command_word(command)
    if cmd_word is None:
        return params

    # Skip past the command word to parse only parameters.
    remaining = command.strip()
    cmd_match = _CMD_RE.match(remaining)
    if cmd_match:
        remaining = remaining[cmd_match.end() :]

    for m in _PARAM_RE.finditer(remaining):
        letter = m.group(1).upper()
        try:
            params[letter] = float(m.group(2))
        except ValueError:
            continue
    return params


def _rebuild_command(cmd_word: str, params: dict[str, float]) -> str:
    """Reconstruct a G-code command from a command word and parameters.

    Parameters are emitted in alphabetical order for deterministic output.
    Float values that are whole numbers are formatted without decimals.
    """
    parts = [cmd_word]
    for letter in sorted(params):
        val = params[letter]
        if val == int(val):
            parts.append(f"{letter}{int(val)}")
        else:
            parts.append(f"{letter}{val:g}")
    return " ".join(parts)


def _strip_comment(line: str) -> str:
    """Remove inline comments (everything from ``;`` onward) and strip."""
    idx = line.find(";")
    if idx != -1:
        line = line[:idx]
    return line.strip()


# ---------------------------------------------------------------------------
# Trigger evaluation helpers
# ---------------------------------------------------------------------------


def _check_temp_exceeds(
    rule: InterceptionRule,
    command: str,
    telemetry: TelemetrySnapshot | None,
) -> bool:
    """Check if a temperature command exceeds the threshold."""
    if rule.threshold is None:
        return False

    cmd = _parse_command_word(command)
    if cmd is None:
        return False

    params = _parse_gcode_params(command)
    s_val = params.get("S")

    if cmd in _HOTEND_TEMP_COMMANDS and s_val is not None:
        return s_val > rule.threshold

    if cmd in _BED_TEMP_COMMANDS and s_val is not None:
        return s_val > rule.threshold

    # Also check live telemetry if available.
    if telemetry is not None:
        if telemetry.hotend_temp is not None and telemetry.hotend_temp > rule.threshold:
            return True
        if telemetry.bed_temp is not None and telemetry.bed_temp > rule.threshold:
            return True

    return False


def _check_temp_below(
    rule: InterceptionRule,
    telemetry: TelemetrySnapshot | None,
) -> bool:
    """Check if current temperatures are below the threshold."""
    if rule.threshold is None or telemetry is None:
        return False

    if telemetry.hotend_temp is not None and telemetry.hotend_temp < rule.threshold:
        return True
    return bool(telemetry.bed_temp is not None and telemetry.bed_temp < rule.threshold)


def _check_temp_delta(
    rule: InterceptionRule,
    telemetry: TelemetrySnapshot | None,
    prev_telemetry: TelemetrySnapshot | None,
) -> bool:
    """Check if temperature is changing too rapidly (thermal runaway)."""
    threshold = rule.threshold if rule.threshold is not None else _DEFAULT_TEMP_DELTA_THRESHOLD
    if telemetry is None or prev_telemetry is None:
        return False

    dt = telemetry.timestamp - prev_telemetry.timestamp
    if dt <= 0:
        return False

    # Check hotend delta.
    if telemetry.hotend_temp is not None and prev_telemetry.hotend_temp is not None:
        delta = abs(telemetry.hotend_temp - prev_telemetry.hotend_temp) / dt
        if delta > threshold:
            return True

    # Check bed delta.
    if telemetry.bed_temp is not None and prev_telemetry.bed_temp is not None:
        delta = abs(telemetry.bed_temp - prev_telemetry.bed_temp) / dt
        if delta > threshold:
            return True

    return False


def _check_feedrate_exceeds(
    rule: InterceptionRule,
    command: str,
) -> bool:
    """Check if a movement command's feedrate exceeds the threshold."""
    if rule.threshold is None:
        return False

    cmd = _parse_command_word(command)
    if cmd not in _MOVE_COMMANDS:
        return False

    params = _parse_gcode_params(command)
    f_val = params.get("F")
    return bool(f_val is not None and f_val > rule.threshold)


def _check_flow_anomaly(
    rule: InterceptionRule,
    telemetry: TelemetrySnapshot | None,
) -> bool:
    """Check if extrusion flow is outside the expected range."""
    if telemetry is None or telemetry.flow_rate_pct is None:
        return False

    low = rule.threshold if rule.threshold is not None else 50.0
    high = rule.threshold_max if rule.threshold_max is not None else 150.0
    return telemetry.flow_rate_pct < low or telemetry.flow_rate_pct > high


def _check_position_limit(
    rule: InterceptionRule,
    command: str,
    telemetry: TelemetrySnapshot | None,
) -> bool:
    """Check if a move command goes outside build volume limits."""
    cmd = _parse_command_word(command)
    if cmd not in _MOVE_COMMANDS:
        return False

    if rule.threshold is None and rule.threshold_max is None:
        return False

    params = _parse_gcode_params(command)

    # Check each axis against threshold (min) and threshold_max (max).
    max_val = rule.threshold_max if rule.threshold_max is not None else float("inf")
    min_val = rule.threshold if rule.threshold is not None else 0.0

    for axis in ("X", "Y", "Z"):
        val = params.get(axis)
        if val is not None and (val < min_val or val > max_val):
            return True

    return False


def _check_command_blocked(
    rule: InterceptionRule,
    command: str,
) -> bool:
    """Check if the command is in the blocked list."""
    cmd = _parse_command_word(command)
    if cmd is None:
        return False

    blocked = rule.blocked_commands or []
    return cmd in blocked


def _check_pattern_match(
    rule: InterceptionRule,
    command: str,
) -> bool:
    """Check if the command matches a regex pattern."""
    if rule.pattern is None:
        return False
    try:
        return bool(re.search(rule.pattern, command, re.IGNORECASE))
    except re.error:
        logger.warning("Invalid regex pattern in rule %s: %s", rule.rule_id, rule.pattern)
        return False


def _check_layer_change(command: str) -> bool:
    """Check if the command line contains a layer-change marker."""
    return bool(_LAYER_CHANGE_RE.search(command))


def _sort_rules_by_priority(rules: list[InterceptionRule]) -> list[InterceptionRule]:
    """Sort rules by priority (CRITICAL first) with stable ordering."""
    return sorted(rules, key=lambda r: _PRIORITY_ORDER.get(r.priority, 99))


# ---------------------------------------------------------------------------
# Core interceptor
# ---------------------------------------------------------------------------


class GcodeInterceptor:
    """Real-time G-code interception engine.

    Thread-safe.  Manages multiple concurrent interception sessions
    (one per printer).  Each session has its own set of rules, telemetry
    state, and command history.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, InterceptionSession] = {}
        self._history: dict[str, deque[InterceptionResult]] = {}
        self._prev_telemetry: dict[str, TelemetrySnapshot] = {}

    # -- session management ------------------------------------------------

    def create_session(
        self,
        printer_name: str,
        *,
        rules: list[InterceptionRule] | None = None,
    ) -> InterceptionSession:
        """Start a new interception session for a printer.

        Optionally accepts initial rules.  If none are provided, auto-loads
        safety-profile-based rules for the printer.

        :param printer_name: Target printer identifier.
        :param rules: Optional initial rule set.
        :returns: The newly created session.
        :raises ValueError: If *printer_name* is empty.
        """
        printer_name = printer_name.strip()
        if not printer_name:
            raise ValueError("printer_name is required")

        session_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        initial_rules: list[InterceptionRule] = []
        if rules is not None:
            initial_rules = list(rules)
        else:
            initial_rules = self.load_safety_rules(printer_name)

        session = InterceptionSession(
            session_id=session_id,
            printer_name=printer_name,
            rules=initial_rules,
            active=True,
            started_at=now,
        )

        with self._lock:
            self._sessions[session_id] = session
            self._history[session_id] = deque(maxlen=_MAX_HISTORY_PER_SESSION)
            self._prev_telemetry.pop(session_id, None)

        logger.info(
            "Interception session created: session=%s printer=%s rules=%d",
            session_id,
            printer_name,
            len(initial_rules),
        )
        return session

    def end_session(self, session_id: str) -> InterceptionSession:
        """End an active interception session.

        :param session_id: Session to end.
        :returns: The ended session with final stats.
        :raises KeyError: If the session does not exist.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")
            session.active = False
            self._prev_telemetry.pop(session_id, None)

        logger.info(
            "Interception session ended: session=%s commands_processed=%d blocked=%d modified=%d",
            session_id,
            session.commands_processed,
            session.commands_blocked,
            session.commands_modified,
        )
        return session

    def get_session(self, session_id: str) -> InterceptionSession | None:
        """Return a session by ID, or ``None`` if not found."""
        with self._lock:
            return self._sessions.get(session_id)

    def get_active_sessions(self) -> list[InterceptionSession]:
        """Return all active interception sessions."""
        with self._lock:
            return [s for s in self._sessions.values() if s.active]

    # -- rule management ---------------------------------------------------

    def add_rule(
        self,
        session_id: str,
        rule: InterceptionRule,
    ) -> InterceptionRule:
        """Add a rule to an active session.

        :param session_id: Target session.
        :param rule: The rule to add.
        :returns: The added rule.
        :raises KeyError: If the session does not exist.
        :raises ValueError: If the session is not active.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")
            if not session.active:
                raise ValueError(f"Session {session_id} is not active")
            session.rules.append(rule)
        return rule

    def remove_rule(self, session_id: str, rule_id: str) -> bool:
        """Remove a rule from an active session.

        :returns: ``True`` if the rule was found and removed.
        :raises KeyError: If the session does not exist.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")
            before = len(session.rules)
            session.rules = [r for r in session.rules if r.rule_id != rule_id]
            return len(session.rules) < before

    # -- telemetry ---------------------------------------------------------

    def update_telemetry(
        self,
        session_id: str,
        telemetry: TelemetrySnapshot,
    ) -> None:
        """Push a new telemetry snapshot for a session.

        The previous snapshot is preserved for delta calculations
        (e.g. temperature rate-of-change).

        :param session_id: Target session.
        :param telemetry: Current device telemetry.
        :raises KeyError: If the session does not exist.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")

            # Preserve previous telemetry for delta triggers.
            if session.last_telemetry is not None:
                self._prev_telemetry[session_id] = session.last_telemetry

            if telemetry.timestamp == 0.0:
                telemetry.timestamp = time.time()
            session.last_telemetry = telemetry

    # -- core interception -------------------------------------------------

    def intercept(self, session_id: str, command: str) -> InterceptionResult:
        """Evaluate a G-code command against all active rules.

        This is the core method.  For each command, all enabled rules
        in the session are evaluated in priority order.  The highest-
        severity action wins (BLOCK > PAUSE > MODIFY > ALERT > ALLOW).

        :param session_id: Active session to evaluate in.
        :param command: Raw G-code command string.
        :returns: :class:`InterceptionResult` with the action to take.
        :raises KeyError: If the session does not exist.
        :raises ValueError: If the session is not active.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")
            if not session.active:
                raise ValueError(f"Session {session_id} is not active")

            telemetry = session.last_telemetry
            prev_telemetry = self._prev_telemetry.get(session_id)
            rules = list(session.rules)

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cleaned = _strip_comment(command)

        # Empty / comment-only lines pass through.
        if not cleaned:
            result = InterceptionResult(
                original_command=command,
                action=InterceptionAction.ALLOW,
                timestamp=now,
            )
            with self._lock:
                session.commands_processed += 1
                self._history.get(session_id, deque()).append(result)
            return result

        # Evaluate all enabled rules sorted by priority.
        sorted_rules = _sort_rules_by_priority([r for r in rules if r.enabled])

        triggered_rules: list[str] = []
        reasons: list[str] = []
        winning_action = InterceptionAction.ALLOW
        modify_params: dict[str, Any] = {}

        for rule in sorted_rules:
            if self._evaluate_trigger(rule, cleaned, telemetry, prev_telemetry):
                triggered_rules.append(rule.rule_id)
                if rule.message:
                    reasons.append(rule.message)
                else:
                    reasons.append(f"Rule '{rule.name}' triggered ({rule.trigger.value})")

                # Determine winning action by precedence.
                if _ACTION_PRECEDENCE.get(rule.action, 99) < _ACTION_PRECEDENCE.get(winning_action, 99):
                    winning_action = rule.action

                # Collect modify params from MODIFY rules.
                if rule.action == InterceptionAction.MODIFY and rule.modify_params:
                    modify_params.update(rule.modify_params)

        # Build modified command if action is MODIFY.
        modified_command: str | None = None
        if winning_action == InterceptionAction.MODIFY and modify_params:
            modified_command = _apply_modification(cleaned, modify_params)

        result = InterceptionResult(
            original_command=command,
            action=winning_action,
            modified_command=modified_command,
            triggered_rules=triggered_rules,
            reasons=reasons,
            timestamp=now,
        )

        # Update session stats.
        with self._lock:
            session.commands_processed += 1
            if winning_action == InterceptionAction.BLOCK:
                session.commands_blocked += 1
            elif winning_action == InterceptionAction.MODIFY:
                session.commands_modified += 1
            elif winning_action == InterceptionAction.PAUSE:
                session.commands_paused += 1
            elif winning_action == InterceptionAction.ALERT:
                session.alerts_issued += 1
            history = self._history.get(session_id)
            if history is not None:
                history.append(result)

        # Emit event for non-ALLOW results.
        if winning_action != InterceptionAction.ALLOW:
            self._emit_event(session, result)

        return result

    # -- safety rule generation --------------------------------------------

    def load_safety_rules(self, printer_name: str) -> list[InterceptionRule]:
        """Auto-generate interception rules from a printer's safety profile.

        Creates rules for:
        - Max hotend temperature (BLOCK)
        - Max bed temperature (BLOCK)
        - Max feedrate (MODIFY to cap)
        - Temperature delta / thermal runaway (ALERT)
        - Default blocked commands (BLOCK)

        :param printer_name: Printer model identifier for profile lookup.
        :returns: List of generated rules.
        """
        rules: list[InterceptionRule] = []
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        profile = None
        try:
            from kiln.safety_profiles import get_profile

            profile = get_profile(printer_name)
        except (ImportError, KeyError):
            logger.debug(
                "No safety profile found for '%s' -- generating default rules only",
                printer_name,
            )

        # -- Hotend temperature limit --
        max_hotend = profile.max_hotend_temp if profile else 300.0
        rules.append(
            InterceptionRule(
                rule_id=str(uuid.uuid4()),
                name=f"max_hotend_temp_{max_hotend:.0f}C",
                trigger=InterceptionTrigger.TEMP_EXCEEDS,
                action=InterceptionAction.BLOCK,
                priority=RulePriority.CRITICAL,
                threshold=max_hotend,
                message=f"Hotend temperature exceeds safety limit ({max_hotend:.0f}C)",
                created_at=now,
            )
        )

        # -- Bed temperature limit --
        max_bed = profile.max_bed_temp if profile else 130.0
        rules.append(
            InterceptionRule(
                rule_id=str(uuid.uuid4()),
                name=f"max_bed_temp_{max_bed:.0f}C",
                trigger=InterceptionTrigger.TEMP_EXCEEDS,
                action=InterceptionAction.BLOCK,
                priority=RulePriority.CRITICAL,
                threshold=max_bed,
                message=f"Bed temperature exceeds safety limit ({max_bed:.0f}C)",
                created_at=now,
            )
        )

        # -- Feedrate limit --
        max_feedrate = profile.max_feedrate if profile else 10_000.0
        rules.append(
            InterceptionRule(
                rule_id=str(uuid.uuid4()),
                name=f"max_feedrate_{max_feedrate:.0f}",
                trigger=InterceptionTrigger.FEEDRATE_EXCEEDS,
                action=InterceptionAction.MODIFY,
                priority=RulePriority.HIGH,
                threshold=max_feedrate,
                modify_params={"F": max_feedrate},
                message=f"Feedrate capped to safety limit ({max_feedrate:.0f} mm/min)",
                created_at=now,
            )
        )

        # -- Thermal runaway detection --
        rules.append(
            InterceptionRule(
                rule_id=str(uuid.uuid4()),
                name="thermal_runaway_detection",
                trigger=InterceptionTrigger.TEMP_DELTA,
                action=InterceptionAction.ALERT,
                priority=RulePriority.CRITICAL,
                threshold=_DEFAULT_TEMP_DELTA_THRESHOLD,
                message="Rapid temperature change detected -- possible thermal runaway",
                created_at=now,
            )
        )

        # -- Blocked commands --
        rules.append(
            InterceptionRule(
                rule_id=str(uuid.uuid4()),
                name="blocked_firmware_commands",
                trigger=InterceptionTrigger.COMMAND_BLOCKED,
                action=InterceptionAction.BLOCK,
                priority=RulePriority.CRITICAL,
                blocked_commands=sorted(_DEFAULT_BLOCKED_COMMANDS),
                message="Command is in the blocked list (firmware-level or safety-critical)",
                created_at=now,
            )
        )

        return rules

    # -- stats & history ---------------------------------------------------

    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        """Return statistics for a session.

        :param session_id: Target session.
        :returns: Dict with command counts and session metadata.
        :raises KeyError: If the session does not exist.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Interception session not found: {session_id}")
            return {
                "session_id": session.session_id,
                "printer_name": session.printer_name,
                "active": session.active,
                "started_at": session.started_at,
                "commands_processed": session.commands_processed,
                "commands_blocked": session.commands_blocked,
                "commands_modified": session.commands_modified,
                "commands_paused": session.commands_paused,
                "alerts_issued": session.alerts_issued,
                "rule_count": len(session.rules),
                "has_telemetry": session.last_telemetry is not None,
            }

    def get_interception_history(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[InterceptionResult]:
        """Return recent interception results for a session.

        :param session_id: Target session.
        :param limit: Maximum results to return.
        :returns: List of results, newest first.
        :raises KeyError: If the session does not exist.
        """
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Interception session not found: {session_id}")
            history = self._history.get(session_id, deque())
            items = list(history)

        items.reverse()
        return items[:limit]

    # -- private helpers ---------------------------------------------------

    def _evaluate_trigger(
        self,
        rule: InterceptionRule,
        command: str,
        telemetry: TelemetrySnapshot | None,
        prev_telemetry: TelemetrySnapshot | None,
    ) -> bool:
        """Check if a rule's trigger condition fires."""
        trigger = rule.trigger

        if trigger == InterceptionTrigger.ALWAYS:
            return True

        if trigger == InterceptionTrigger.TEMP_EXCEEDS:
            return _check_temp_exceeds(rule, command, telemetry)

        if trigger == InterceptionTrigger.TEMP_BELOW:
            return _check_temp_below(rule, telemetry)

        if trigger == InterceptionTrigger.TEMP_DELTA:
            return _check_temp_delta(rule, telemetry, prev_telemetry)

        if trigger == InterceptionTrigger.FEEDRATE_EXCEEDS:
            return _check_feedrate_exceeds(rule, command)

        if trigger == InterceptionTrigger.FLOW_ANOMALY:
            return _check_flow_anomaly(rule, telemetry)

        if trigger == InterceptionTrigger.POSITION_LIMIT:
            return _check_position_limit(rule, command, telemetry)

        if trigger == InterceptionTrigger.COMMAND_BLOCKED:
            return _check_command_blocked(rule, command)

        if trigger == InterceptionTrigger.PATTERN_MATCH:
            return _check_pattern_match(rule, command)

        if trigger == InterceptionTrigger.LAYER_CHANGE:
            return _check_layer_change(command)

        return False

    def _emit_event(
        self,
        session: InterceptionSession,
        result: InterceptionResult,
    ) -> None:
        """Best-effort event emission for interception actions."""
        try:
            from kiln.events import Event, EventBus, EventType

            event = Event(
                type=EventType.SAFETY_BLOCKED,
                data={
                    "session_id": session.session_id,
                    "printer_name": session.printer_name,
                    "action": result.action.value,
                    "original_command": result.original_command,
                    "modified_command": result.modified_command,
                    "triggered_rules": result.triggered_rules,
                    "reasons": result.reasons,
                },
                source=f"interceptor:{session.printer_name}",
            )

            bus: EventBus | None = None
            try:
                from kiln.server import _event_bus as server_bus

                bus = server_bus
            except ImportError:
                pass

            if bus is not None:
                bus.publish(event)
        except Exception:
            logger.debug(
                "Failed to emit interception event for session %s",
                session.session_id,
                exc_info=True,
            )


def _apply_modification(command: str, modify_params: dict[str, Any]) -> str:
    """Rewrite G-code parameters based on modification rules.

    For each key in *modify_params*, if the parameter exists in the
    command, its value is replaced.  The command word is preserved.
    """
    cmd_word = _parse_command_word(command)
    if cmd_word is None:
        return command

    params = _parse_gcode_params(command)

    for key, value in modify_params.items():
        key_upper = key.upper()
        # Only modify if the parameter exists in the original command,
        # or if the modification should cap an existing value.
        if key_upper in params:
            try:
                new_val = float(value)
                # For feedrate and temp caps, take the minimum.
                if key_upper == "F" or key_upper == "S":
                    params[key_upper] = min(params[key_upper], new_val)
                else:
                    params[key_upper] = new_val
            except (TypeError, ValueError):
                continue
        else:
            # Add the parameter if not present (for certain overrides).
            try:
                params[key_upper] = float(value)
            except (TypeError, ValueError):
                continue

    return _rebuild_command(cmd_word, params)


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_interceptor: GcodeInterceptor | None = None
_interceptor_lock = threading.Lock()


def get_interceptor() -> GcodeInterceptor:
    """Return the module-level :class:`GcodeInterceptor` singleton.

    Created lazily on first access.
    """
    global _interceptor
    if _interceptor is None:
        with _interceptor_lock:
            if _interceptor is None:
                _interceptor = GcodeInterceptor()
    return _interceptor
