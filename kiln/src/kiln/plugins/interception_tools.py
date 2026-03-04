"""G-code interception tool plugin.

Provides MCP tools for real-time G-code interception with AI override.
Agents can start interception sessions for printers, add custom rules,
push telemetry, and evaluate commands before they reach the hardware.

Discovered and registered automatically by
:func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standalone functions — importable for direct calls and testing.
# ---------------------------------------------------------------------------


def start_gcode_interception(
    printer_name: str,
) -> dict[str, Any]:
    """Start a real-time G-code interception session for a printer.

    Automatically loads safety-profile-based rules (temperature limits,
    feedrate caps, blocked commands) for the printer.  Use
    ``add_interception_rule`` to add custom rules after starting.

    Args:
        printer_name: Target printer name (e.g. "ender3", "voron-350").

    Returns a session ID and initial rule set.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        session = interceptor.create_session(printer_name)
        return {
            "success": True,
            "session": session.to_dict(),
            "message": f"Interception session started for '{printer_name}' with {len(session.rules)} safety rules.",
        }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in start_gcode_interception")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def stop_gcode_interception(
    session_id: str,
) -> dict[str, Any]:
    """Stop a G-code interception session.

    Args:
        session_id: The session ID returned by ``start_gcode_interception``.

    Returns the final session stats.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        session = interceptor.end_session(session_id)
        return {
            "success": True,
            "session": session.to_dict(),
            "message": f"Interception session ended. Processed {session.commands_processed} commands.",
        }
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in stop_gcode_interception")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def add_interception_rule(
    session_id: str,
    name: str,
    trigger: str,
    action: str,
    priority: str = "medium",
    threshold: float | None = None,
    threshold_max: float | None = None,
    blocked_commands: list[str] | None = None,
    pattern: str | None = None,
    modify_params: dict[str, Any] | None = None,
    message: str = "",
) -> dict[str, Any]:
    """Add a custom interception rule to an active session.

    Args:
        session_id: Target session ID.
        name: Human-readable rule name.
        trigger: Trigger type -- one of: temp_exceeds, temp_below,
            temp_delta, feedrate_exceeds, flow_anomaly, position_limit,
            command_blocked, pattern_match, always, layer_change.
        action: Action to take -- one of: allow, block, modify, pause, alert.
        priority: Rule priority -- one of: critical, high, medium, low.
        threshold: Numeric threshold for trigger evaluation.
        threshold_max: Upper bound for range-based triggers.
        blocked_commands: List of G/M codes for command_blocked trigger.
        pattern: Regex pattern for pattern_match trigger.
        modify_params: Parameter overrides for modify action (e.g. {"F": 3000}).
        message: Human-readable explanation shown when rule fires.
    """
    from kiln.gcode_interceptor import (
        InterceptionAction,
        InterceptionRule,
        InterceptionTrigger,
        RulePriority,
        get_interceptor,
    )

    try:
        trigger_enum = InterceptionTrigger(trigger)
    except ValueError:
        return {"success": False, "error": f"Invalid trigger: {trigger!r}"}

    try:
        action_enum = InterceptionAction(action)
    except ValueError:
        return {"success": False, "error": f"Invalid action: {action!r}"}

    try:
        priority_enum = RulePriority(priority)
    except ValueError:
        return {"success": False, "error": f"Invalid priority: {priority!r}"}

    rule = InterceptionRule(
        rule_id=str(uuid.uuid4()),
        name=name,
        trigger=trigger_enum,
        action=action_enum,
        priority=priority_enum,
        threshold=threshold,
        threshold_max=threshold_max,
        blocked_commands=blocked_commands,
        pattern=pattern,
        modify_params=modify_params,
        message=message,
        enabled=True,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    try:
        interceptor = get_interceptor()
        added = interceptor.add_rule(session_id, rule)
        return {
            "success": True,
            "rule": added.to_dict(),
            "message": f"Rule '{name}' added to session.",
        }
    except (KeyError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in add_interception_rule")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def remove_interception_rule(
    session_id: str,
    rule_id: str,
) -> dict[str, Any]:
    """Remove an interception rule from an active session.

    Args:
        session_id: Target session ID.
        rule_id: The rule ID to remove.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        removed = interceptor.remove_rule(session_id, rule_id)
        if removed:
            return {"success": True, "message": f"Rule {rule_id} removed."}
        return {"success": False, "error": f"Rule {rule_id} not found in session."}
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in remove_interception_rule")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def intercept_gcode_command(
    session_id: str,
    command: str,
) -> dict[str, Any]:
    """Evaluate a G-code command against interception rules.

    This is the core tool.  Pass each G-code command through this
    before sending to the printer.  The result tells you whether to
    ALLOW, BLOCK, MODIFY, PAUSE, or ALERT.

    Args:
        session_id: Active interception session ID.
        command: Raw G-code command string (e.g. "M104 S280", "G1 X10 F6000").

    Returns the interception result including action, modified command
    (if applicable), triggered rules, and human-readable reasons.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        result = interceptor.intercept(session_id, command)
        return {
            "success": True,
            "result": result.to_dict(),
        }
    except (KeyError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in intercept_gcode_command")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def update_interception_telemetry(
    session_id: str,
    hotend_temp: float | None = None,
    hotend_target: float | None = None,
    bed_temp: float | None = None,
    bed_target: float | None = None,
    position_x: float | None = None,
    position_y: float | None = None,
    position_z: float | None = None,
    feedrate: float | None = None,
    flow_rate_pct: float | None = None,
    fan_speed_pct: float | None = None,
    current_layer: int | None = None,
    elapsed_seconds: float | None = None,
    filament_used_mm: float | None = None,
) -> dict[str, Any]:
    """Push a telemetry snapshot to an active interception session.

    Telemetry is used by rules that evaluate live device state
    (temperature thresholds, thermal runaway detection, flow anomalies).
    Call this periodically during printing (e.g. every few seconds).

    Args:
        session_id: Active session ID.
        hotend_temp: Current hotend temperature (C).
        hotend_target: Target hotend temperature (C).
        bed_temp: Current bed temperature (C).
        bed_target: Target bed temperature (C).
        position_x: Current X position (mm).
        position_y: Current Y position (mm).
        position_z: Current Z position (mm).
        feedrate: Current feedrate (mm/min).
        flow_rate_pct: Flow rate percentage (100 = normal).
        fan_speed_pct: Fan speed percentage.
        current_layer: Current layer number.
        elapsed_seconds: Elapsed print time (seconds).
        filament_used_mm: Filament consumed (mm).
    """
    from kiln.gcode_interceptor import TelemetrySnapshot, get_interceptor

    telemetry = TelemetrySnapshot(
        hotend_temp=hotend_temp,
        hotend_target=hotend_target,
        bed_temp=bed_temp,
        bed_target=bed_target,
        position_x=position_x,
        position_y=position_y,
        position_z=position_z,
        feedrate=feedrate,
        flow_rate_pct=flow_rate_pct,
        fan_speed_pct=fan_speed_pct,
        current_layer=current_layer,
        elapsed_seconds=elapsed_seconds,
        filament_used_mm=filament_used_mm,
    )

    try:
        interceptor = get_interceptor()
        interceptor.update_telemetry(session_id, telemetry)
        return {
            "success": True,
            "message": "Telemetry updated.",
            "telemetry": telemetry.to_dict(),
        }
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in update_interception_telemetry")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def get_interception_status(
    session_id: str,
) -> dict[str, Any]:
    """Get status and statistics for an interception session.

    Args:
        session_id: Target session ID.

    Returns session metadata, command counts, and current telemetry.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        stats = interceptor.get_session_stats(session_id)
        return {
            "success": True,
            "stats": stats,
        }
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in get_interception_status")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def list_interception_sessions() -> dict[str, Any]:
    """List all active G-code interception sessions.

    Returns summary information for each active session including
    printer name, command counts, and rule counts.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        sessions = interceptor.get_active_sessions()
        return {
            "success": True,
            "sessions": [s.to_dict() for s in sessions],
            "count": len(sessions),
        }
    except Exception as exc:
        _logger.exception("Unexpected error in list_interception_sessions")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def load_safety_interception_rules(
    session_id: str,
    printer_name: str,
) -> dict[str, Any]:
    """Load safety-profile rules for a printer into an active session.

    Generates rules from the printer's safety profile (temperature
    limits, feedrate caps, blocked commands) and adds them to the
    session.  Use this to reset or refresh safety rules.

    Args:
        session_id: Target session ID.
        printer_name: Printer model for safety profile lookup.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        interceptor = get_interceptor()
        rules = interceptor.load_safety_rules(printer_name)
        for rule in rules:
            interceptor.add_rule(session_id, rule)
        return {
            "success": True,
            "rules_added": len(rules),
            "rules": [r.to_dict() for r in rules],
            "message": f"Loaded {len(rules)} safety rules for '{printer_name}'.",
        }
    except (KeyError, ValueError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in load_safety_interception_rules")
        return {"success": False, "error": f"Unexpected error: {exc}"}


def get_interception_history(
    session_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Get recent interception results for a session.

    Args:
        session_id: Target session ID.
        limit: Maximum results to return (default 50, max 500).

    Returns interception results from newest to oldest, including
    actions taken, triggered rules, and reasons.
    """
    from kiln.gcode_interceptor import get_interceptor

    try:
        capped = min(max(limit, 1), 500)
        interceptor = get_interceptor()
        history = interceptor.get_interception_history(session_id, limit=capped)
        return {
            "success": True,
            "results": [r.to_dict() for r in history],
            "count": len(history),
        }
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        _logger.exception("Unexpected error in get_interception_history")
        return {"success": False, "error": f"Unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# Plugin class — registers standalone functions as MCP tools.
# ---------------------------------------------------------------------------


class _InterceptionToolsPlugin:
    """MCP tools for real-time G-code interception with AI override.

    Covers session management, rule configuration, telemetry updates,
    command interception, and history retrieval.
    """

    @property
    def name(self) -> str:
        return "interception_tools"

    @property
    def description(self) -> str:
        return "Real-time G-code interception tools (intercept, block, modify, alert)"

    def register(self, mcp: Any) -> None:
        """Register interception tools with the MCP server."""
        mcp.tool()(start_gcode_interception)
        mcp.tool()(stop_gcode_interception)
        mcp.tool()(add_interception_rule)
        mcp.tool()(remove_interception_rule)
        mcp.tool()(intercept_gcode_command)
        mcp.tool()(update_interception_telemetry)
        mcp.tool()(get_interception_status)
        mcp.tool()(list_interception_sessions)
        mcp.tool()(load_safety_interception_rules)
        mcp.tool()(get_interception_history)
        _logger.debug("Registered G-code interception tools")


plugin = _InterceptionToolsPlugin()
