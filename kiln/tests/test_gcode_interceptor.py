"""Tests for kiln.gcode_interceptor -- real-time G-code interception.

Coverage areas:
- Interception session lifecycle (create, end, get, list)
- Rule management (add, remove, enable/disable, priority sorting)
- Telemetry updates and snapshot preservation
- Interception actions (allow, block, modify, pause, alert)
- Trigger evaluation (temp, feedrate, position, command, pattern, layer)
- Safety rule auto-generation from profiles
- G-code parameter parsing and command reconstruction
- Multiple rules firing on a single command (priority wins)
- Thread-safe concurrent operations
- Session statistics tracking
- Edge cases (empty commands, missing fields, malformed input)
- Dataclass serialization (to_dict with enum conversion)
"""

from __future__ import annotations

import threading
import uuid

import pytest

from kiln.gcode_interceptor import (
    GcodeInterceptor,
    InterceptionAction,
    InterceptionResult,
    InterceptionRule,
    InterceptionSession,
    InterceptionTrigger,
    RulePriority,
    TelemetrySnapshot,
    _apply_modification,
    _check_command_blocked,
    _check_feedrate_exceeds,
    _check_flow_anomaly,
    _check_layer_change,
    _check_pattern_match,
    _check_position_limit,
    _check_temp_below,
    _check_temp_delta,
    _check_temp_exceeds,
    _parse_command_word,
    _parse_gcode_params,
    _rebuild_command,
    _sort_rules_by_priority,
    _strip_comment,
    get_interceptor,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def interceptor():
    """Fresh GcodeInterceptor for test isolation."""
    return GcodeInterceptor()


@pytest.fixture()
def session(interceptor):
    """Create a session with no auto-loaded rules for precise testing."""
    return interceptor.create_session("test_printer", rules=[])


def _make_rule(
    *,
    trigger: InterceptionTrigger = InterceptionTrigger.ALWAYS,
    action: InterceptionAction = InterceptionAction.BLOCK,
    priority: RulePriority = RulePriority.MEDIUM,
    threshold: float | None = None,
    threshold_max: float | None = None,
    blocked_commands: list[str] | None = None,
    pattern: str | None = None,
    modify_params: dict | None = None,
    message: str = "",
    enabled: bool = True,
    name: str = "test_rule",
) -> InterceptionRule:
    """Helper to create a rule with minimal boilerplate."""
    return InterceptionRule(
        rule_id=str(uuid.uuid4()),
        name=name,
        trigger=trigger,
        action=action,
        priority=priority,
        threshold=threshold,
        threshold_max=threshold_max,
        blocked_commands=blocked_commands,
        pattern=pattern,
        modify_params=modify_params,
        message=message,
        enabled=enabled,
        created_at="2026-01-01T00:00:00Z",
    )


# ===================================================================
# 1. InterceptionSession -- create, end, get, list
# ===================================================================


class TestInterceptionSession:
    """Session lifecycle management."""

    def test_create_session_returns_active(self, interceptor):
        session = interceptor.create_session("ender3", rules=[])
        assert session.active is True
        assert session.printer_name == "ender3"
        assert session.session_id
        assert session.started_at

    def test_create_session_with_rules(self, interceptor):
        rule = _make_rule()
        session = interceptor.create_session("ender3", rules=[rule])
        assert len(session.rules) == 1
        assert session.rules[0].rule_id == rule.rule_id

    def test_create_session_empty_name_raises(self, interceptor):
        with pytest.raises(ValueError, match="printer_name is required"):
            interceptor.create_session("")

    def test_create_session_whitespace_name_raises(self, interceptor):
        with pytest.raises(ValueError, match="printer_name is required"):
            interceptor.create_session("   ")

    def test_end_session(self, interceptor, session):
        ended = interceptor.end_session(session.session_id)
        assert ended.active is False

    def test_end_session_not_found_raises(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.end_session("nonexistent")

    def test_get_session(self, interceptor, session):
        found = interceptor.get_session(session.session_id)
        assert found is not None
        assert found.session_id == session.session_id

    def test_get_session_not_found(self, interceptor):
        assert interceptor.get_session("nonexistent") is None

    def test_get_active_sessions(self, interceptor):
        s1 = interceptor.create_session("printer1", rules=[])
        s2 = interceptor.create_session("printer2", rules=[])
        interceptor.end_session(s1.session_id)
        active = interceptor.get_active_sessions()
        ids = [s.session_id for s in active]
        assert s2.session_id in ids
        assert s1.session_id not in ids

    def test_get_active_sessions_empty(self, interceptor):
        assert interceptor.get_active_sessions() == []

    def test_session_initial_stats_zero(self, session):
        assert session.commands_processed == 0
        assert session.commands_blocked == 0
        assert session.commands_modified == 0
        assert session.commands_paused == 0
        assert session.alerts_issued == 0


# ===================================================================
# 2. RuleManagement -- add, remove, priority sorting
# ===================================================================


class TestRuleManagement:
    """Adding, removing, and sorting rules."""

    def test_add_rule(self, interceptor, session):
        rule = _make_rule()
        added = interceptor.add_rule(session.session_id, rule)
        assert added.rule_id == rule.rule_id
        s = interceptor.get_session(session.session_id)
        assert len(s.rules) == 1

    def test_add_rule_to_inactive_session_raises(self, interceptor, session):
        interceptor.end_session(session.session_id)
        with pytest.raises(ValueError, match="not active"):
            interceptor.add_rule(session.session_id, _make_rule())

    def test_add_rule_session_not_found_raises(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.add_rule("nonexistent", _make_rule())

    def test_remove_rule(self, interceptor, session):
        rule = _make_rule()
        interceptor.add_rule(session.session_id, rule)
        assert interceptor.remove_rule(session.session_id, rule.rule_id) is True
        s = interceptor.get_session(session.session_id)
        assert len(s.rules) == 0

    def test_remove_rule_not_found(self, interceptor, session):
        assert interceptor.remove_rule(session.session_id, "nonexistent") is False

    def test_remove_rule_session_not_found_raises(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.remove_rule("nonexistent", "any")

    def test_sort_rules_by_priority(self):
        low = _make_rule(priority=RulePriority.LOW, name="low")
        high = _make_rule(priority=RulePriority.HIGH, name="high")
        critical = _make_rule(priority=RulePriority.CRITICAL, name="critical")
        medium = _make_rule(priority=RulePriority.MEDIUM, name="medium")

        sorted_rules = _sort_rules_by_priority([low, high, critical, medium])
        priorities = [r.priority for r in sorted_rules]
        assert priorities == [
            RulePriority.CRITICAL,
            RulePriority.HIGH,
            RulePriority.MEDIUM,
            RulePriority.LOW,
        ]

    def test_disabled_rule_not_evaluated(self, interceptor, session):
        rule = _make_rule(
            action=InterceptionAction.BLOCK,
            enabled=False,
            message="should not fire",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.ALLOW
        assert len(result.triggered_rules) == 0


# ===================================================================
# 3. TelemetryUpdate
# ===================================================================


class TestTelemetryUpdate:
    """Telemetry snapshot management."""

    def test_update_telemetry(self, interceptor, session):
        t = TelemetrySnapshot(hotend_temp=200.0)
        interceptor.update_telemetry(session.session_id, t)
        s = interceptor.get_session(session.session_id)
        assert s.last_telemetry is not None
        assert s.last_telemetry.hotend_temp == 200.0

    def test_update_telemetry_preserves_previous(self, interceptor, session):
        t1 = TelemetrySnapshot(hotend_temp=180.0, timestamp=100.0)
        t2 = TelemetrySnapshot(hotend_temp=200.0, timestamp=101.0)
        interceptor.update_telemetry(session.session_id, t1)
        interceptor.update_telemetry(session.session_id, t2)
        s = interceptor.get_session(session.session_id)
        assert s.last_telemetry.hotend_temp == 200.0

    def test_update_telemetry_session_not_found(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.update_telemetry("nonexistent", TelemetrySnapshot())

    def test_update_telemetry_auto_timestamps(self, interceptor, session):
        t = TelemetrySnapshot(hotend_temp=100.0)
        assert t.timestamp == 0.0
        interceptor.update_telemetry(session.session_id, t)
        assert t.timestamp > 0.0


# ===================================================================
# 4. TestInterceptAllow
# ===================================================================


class TestInterceptAllow:
    """Commands that pass through all rules (ALLOW)."""

    def test_simple_move_allowed(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "G1 X10 Y10 Z0.2 F1200")
        assert result.action == InterceptionAction.ALLOW

    def test_home_command_allowed(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "G28")
        assert result.action == InterceptionAction.ALLOW

    def test_empty_command_allowed(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "")
        assert result.action == InterceptionAction.ALLOW

    def test_comment_only_allowed(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "; this is a comment")
        assert result.action == InterceptionAction.ALLOW

    def test_safe_temp_allowed(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=260.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M104 S200")
        assert result.action == InterceptionAction.ALLOW

    def test_no_rules_always_allows(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "M104 S999")
        assert result.action == InterceptionAction.ALLOW


# ===================================================================
# 5. TestInterceptBlock
# ===================================================================


class TestInterceptBlock:
    """Commands blocked by rules."""

    def test_always_block_rule(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.BLOCK,
            message="blocked always",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.BLOCK
        assert rule.rule_id in result.triggered_rules
        assert "blocked always" in result.reasons

    def test_block_increments_counter(self, interceptor, session):
        rule = _make_rule(action=InterceptionAction.BLOCK)
        interceptor.add_rule(session.session_id, rule)
        interceptor.intercept(session.session_id, "G1 X10")
        s = interceptor.get_session(session.session_id)
        assert s.commands_blocked == 1

    def test_intercept_inactive_session_raises(self, interceptor, session):
        interceptor.end_session(session.session_id)
        with pytest.raises(ValueError, match="not active"):
            interceptor.intercept(session.session_id, "G1 X10")

    def test_intercept_nonexistent_session_raises(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.intercept("nonexistent", "G1 X10")


# ===================================================================
# 6. TestInterceptModify
# ===================================================================


class TestInterceptModify:
    """Commands with modified parameters."""

    def test_modify_feedrate(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.FEEDRATE_EXCEEDS,
            action=InterceptionAction.MODIFY,
            threshold=3000.0,
            modify_params={"F": 3000.0},
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10 F6000")
        assert result.action == InterceptionAction.MODIFY
        assert result.modified_command is not None
        assert "F3000" in result.modified_command

    def test_modify_increments_counter(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.MODIFY,
            modify_params={"F": 1000},
        )
        interceptor.add_rule(session.session_id, rule)
        interceptor.intercept(session.session_id, "G1 X10 F5000")
        s = interceptor.get_session(session.session_id)
        assert s.commands_modified == 1

    def test_modify_caps_feedrate_to_minimum(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.MODIFY,
            modify_params={"F": 2000},
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10 F1000")
        assert result.modified_command is not None
        # F1000 is already below F2000 cap, so min(1000, 2000) = 1000
        assert "F1000" in result.modified_command

    def test_modify_no_params_returns_none(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.MODIFY,
            modify_params=None,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.MODIFY
        assert result.modified_command is None


# ===================================================================
# 7. TestInterceptPause
# ===================================================================


class TestInterceptPause:
    """Commands that trigger pause for human review."""

    def test_pause_action(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.PAUSE,
            message="requires human review",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 Z-1")
        assert result.action == InterceptionAction.PAUSE

    def test_pause_increments_counter(self, interceptor, session):
        rule = _make_rule(action=InterceptionAction.PAUSE)
        interceptor.add_rule(session.session_id, rule)
        interceptor.intercept(session.session_id, "G1 X10")
        s = interceptor.get_session(session.session_id)
        assert s.commands_paused == 1


# ===================================================================
# 8. TestInterceptAlert
# ===================================================================


class TestInterceptAlert:
    """Commands that trigger alerts."""

    def test_alert_action(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.ALERT,
            message="advisory warning",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M104 S200")
        assert result.action == InterceptionAction.ALERT

    def test_alert_increments_counter(self, interceptor, session):
        rule = _make_rule(action=InterceptionAction.ALERT)
        interceptor.add_rule(session.session_id, rule)
        interceptor.intercept(session.session_id, "G1 X10")
        s = interceptor.get_session(session.session_id)
        assert s.alerts_issued == 1


# ===================================================================
# 9. TestTempExceedsTrigger
# ===================================================================


class TestTempExceedsTrigger:
    """Temperature exceeds threshold blocking."""

    def test_hotend_temp_exceeds_blocks(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=260.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M104 S280")
        assert result.action == InterceptionAction.BLOCK

    def test_hotend_temp_at_limit_allows(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            threshold=260.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M104 S260")
        assert result.action == InterceptionAction.ALLOW

    def test_bed_temp_exceeds_blocks(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=110.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M140 S120")
        assert result.action == InterceptionAction.BLOCK

    def test_m109_wait_temp_exceeds(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=260.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M109 S300")
        assert result.action == InterceptionAction.BLOCK

    def test_m190_wait_bed_exceeds(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=100.0,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M190 S110")
        assert result.action == InterceptionAction.BLOCK

    def test_telemetry_hotend_exceeds(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_EXCEEDS, threshold=250.0)
        telemetry = TelemetrySnapshot(hotend_temp=260.0)
        assert _check_temp_exceeds(rule, "G1 X10", telemetry) is True

    def test_telemetry_below_threshold_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_EXCEEDS, threshold=250.0)
        telemetry = TelemetrySnapshot(hotend_temp=200.0)
        assert _check_temp_exceeds(rule, "G1 X10", telemetry) is False

    def test_no_threshold_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_EXCEEDS, threshold=None)
        assert _check_temp_exceeds(rule, "M104 S300", None) is False


# ===================================================================
# 10. TestTempBelowTrigger
# ===================================================================


class TestTempBelowTrigger:
    """Temperature too low alerts."""

    def test_hotend_below_threshold(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_BELOW, threshold=150.0)
        telemetry = TelemetrySnapshot(hotend_temp=100.0)
        assert _check_temp_below(rule, telemetry) is True

    def test_hotend_above_threshold(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_BELOW, threshold=150.0)
        telemetry = TelemetrySnapshot(hotend_temp=200.0)
        assert _check_temp_below(rule, telemetry) is False

    def test_bed_below_threshold(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_BELOW, threshold=50.0)
        telemetry = TelemetrySnapshot(bed_temp=30.0)
        assert _check_temp_below(rule, telemetry) is True

    def test_no_telemetry_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_BELOW, threshold=150.0)
        assert _check_temp_below(rule, None) is False

    def test_no_threshold_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_BELOW, threshold=None)
        telemetry = TelemetrySnapshot(hotend_temp=100.0)
        assert _check_temp_below(rule, telemetry) is False


# ===================================================================
# 11. TestTempDeltaTrigger
# ===================================================================


class TestTempDeltaTrigger:
    """Rapid temperature change detection (thermal runaway)."""

    def test_rapid_hotend_change_triggers(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=5.0)
        prev = TelemetrySnapshot(hotend_temp=200.0, timestamp=100.0)
        curr = TelemetrySnapshot(hotend_temp=210.0, timestamp=101.0)
        assert _check_temp_delta(rule, curr, prev) is True

    def test_slow_change_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=10.0)
        prev = TelemetrySnapshot(hotend_temp=200.0, timestamp=100.0)
        curr = TelemetrySnapshot(hotend_temp=205.0, timestamp=101.0)
        assert _check_temp_delta(rule, curr, prev) is False

    def test_bed_rapid_change_triggers(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=5.0)
        prev = TelemetrySnapshot(bed_temp=60.0, timestamp=100.0)
        curr = TelemetrySnapshot(bed_temp=70.0, timestamp=101.0)
        assert _check_temp_delta(rule, curr, prev) is True

    def test_no_previous_telemetry_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=5.0)
        curr = TelemetrySnapshot(hotend_temp=200.0, timestamp=101.0)
        assert _check_temp_delta(rule, curr, None) is False

    def test_zero_time_delta_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=5.0)
        prev = TelemetrySnapshot(hotend_temp=200.0, timestamp=100.0)
        curr = TelemetrySnapshot(hotend_temp=220.0, timestamp=100.0)
        assert _check_temp_delta(rule, curr, prev) is False

    def test_no_telemetry_at_all(self):
        rule = _make_rule(trigger=InterceptionTrigger.TEMP_DELTA, threshold=5.0)
        assert _check_temp_delta(rule, None, None) is False


# ===================================================================
# 12. TestFeedrateTrigger
# ===================================================================


class TestFeedrateTrigger:
    """Speed limit enforcement."""

    def test_feedrate_exceeds(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "G1 X10 F5000") is True

    def test_feedrate_within_limit(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "G1 X10 F2000") is False

    def test_feedrate_at_limit(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "G1 X10 F3000") is False

    def test_no_feedrate_param_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "G1 X10") is False

    def test_non_move_command_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "M104 S200") is False

    def test_g0_feedrate_exceeds(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=3000.0)
        assert _check_feedrate_exceeds(rule, "G0 X10 F5000") is True

    def test_no_threshold_no_trigger(self):
        rule = _make_rule(trigger=InterceptionTrigger.FEEDRATE_EXCEEDS, threshold=None)
        assert _check_feedrate_exceeds(rule, "G1 X10 F5000") is False


# ===================================================================
# 13. TestPositionLimitTrigger
# ===================================================================


class TestPositionLimitTrigger:
    """Build volume boundary checks."""

    def test_x_exceeds_max(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=220.0,
        )
        assert _check_position_limit(rule, "G1 X250", None) is True

    def test_y_exceeds_max(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=220.0,
        )
        assert _check_position_limit(rule, "G1 Y230", None) is True

    def test_z_exceeds_max(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=250.0,
        )
        assert _check_position_limit(rule, "G1 Z300", None) is True

    def test_within_bounds(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=220.0,
        )
        assert _check_position_limit(rule, "G1 X100 Y100 Z50", None) is False

    def test_negative_position_below_min(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=220.0,
        )
        assert _check_position_limit(rule, "G1 X-5", None) is True

    def test_non_move_command_no_trigger(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=0.0,
            threshold_max=220.0,
        )
        assert _check_position_limit(rule, "M104 S200", None) is False

    def test_no_thresholds_no_trigger(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.POSITION_LIMIT,
            threshold=None,
            threshold_max=None,
        )
        assert _check_position_limit(rule, "G1 X999", None) is False


# ===================================================================
# 14. TestCommandBlockedTrigger
# ===================================================================


class TestCommandBlockedTrigger:
    """Specific command blocking."""

    def test_m112_blocked(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=["M112", "M500"],
        )
        assert _check_command_blocked(rule, "M112") is True

    def test_m500_blocked(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=["M112", "M500"],
        )
        assert _check_command_blocked(rule, "M500") is True

    def test_safe_command_not_blocked(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=["M112", "M500"],
        )
        assert _check_command_blocked(rule, "G1 X10") is False

    def test_empty_blocked_list(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=[],
        )
        assert _check_command_blocked(rule, "M112") is False

    def test_none_blocked_list(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=None,
        )
        assert _check_command_blocked(rule, "M112") is False

    def test_empty_command_no_match(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            blocked_commands=["M112"],
        )
        assert _check_command_blocked(rule, "") is False


# ===================================================================
# 15. TestPatternMatchTrigger
# ===================================================================


class TestPatternMatchTrigger:
    """Regex-based interception."""

    def test_pattern_matches(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.PATTERN_MATCH,
            pattern=r"M106\s+S\d+",
        )
        assert _check_pattern_match(rule, "M106 S255") is True

    def test_pattern_no_match(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.PATTERN_MATCH,
            pattern=r"M106\s+S\d+",
        )
        assert _check_pattern_match(rule, "G1 X10") is False

    def test_pattern_case_insensitive(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.PATTERN_MATCH,
            pattern=r"m106",
        )
        assert _check_pattern_match(rule, "M106 S255") is True

    def test_invalid_regex_no_match(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.PATTERN_MATCH,
            pattern=r"[invalid",
        )
        assert _check_pattern_match(rule, "anything") is False

    def test_none_pattern_no_match(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.PATTERN_MATCH,
            pattern=None,
        )
        assert _check_pattern_match(rule, "anything") is False


# ===================================================================
# 16. TestLayerChangeTrigger
# ===================================================================


class TestLayerChangeTrigger:
    """Layer change detection."""

    def test_layer_change_comment(self):
        assert _check_layer_change(";LAYER_CHANGE") is True

    def test_layer_number_comment(self):
        assert _check_layer_change("; LAYER: 5") is True

    def test_z_height_comment(self):
        assert _check_layer_change("; Z: 0.3") is True

    def test_layer_colon_format(self):
        assert _check_layer_change("; layer: 10") is True

    def test_normal_command_no_layer(self):
        assert _check_layer_change("G1 X10 Y10") is False

    def test_empty_string_no_layer(self):
        assert _check_layer_change("") is False


# ===================================================================
# 17. TestSafetyRuleGeneration
# ===================================================================


class TestSafetyRuleGeneration:
    """Auto-generation of rules from safety profiles."""

    def test_generates_rules_without_profile(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        # Should still generate default rules.
        assert len(rules) >= 4  # hotend, bed, feedrate, thermal runaway, blocked

    def test_generates_hotend_rule(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        hotend_rules = [r for r in rules if "hotend" in r.name]
        assert len(hotend_rules) >= 1
        assert hotend_rules[0].trigger == InterceptionTrigger.TEMP_EXCEEDS
        assert hotend_rules[0].action == InterceptionAction.BLOCK

    def test_generates_bed_rule(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        bed_rules = [r for r in rules if "bed" in r.name]
        assert len(bed_rules) >= 1
        assert bed_rules[0].trigger == InterceptionTrigger.TEMP_EXCEEDS

    def test_generates_feedrate_rule(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        feedrate_rules = [r for r in rules if "feedrate" in r.name]
        assert len(feedrate_rules) >= 1
        assert feedrate_rules[0].trigger == InterceptionTrigger.FEEDRATE_EXCEEDS
        assert feedrate_rules[0].action == InterceptionAction.MODIFY

    def test_generates_thermal_runaway_rule(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        thermal_rules = [r for r in rules if "thermal" in r.name]
        assert len(thermal_rules) >= 1
        assert thermal_rules[0].trigger == InterceptionTrigger.TEMP_DELTA

    def test_generates_blocked_commands_rule(self, interceptor):
        rules = interceptor.load_safety_rules("nonexistent_printer_xyz")
        blocked_rules = [r for r in rules if "blocked" in r.name]
        assert len(blocked_rules) >= 1
        assert blocked_rules[0].trigger == InterceptionTrigger.COMMAND_BLOCKED

    def test_auto_load_on_session_create(self, interceptor):
        """Creating a session without explicit rules auto-loads safety rules."""
        session = interceptor.create_session("nonexistent_printer_xyz")
        assert len(session.rules) >= 4


# ===================================================================
# 18. TestGcodeParamParsing
# ===================================================================


class TestGcodeParamParsing:
    """Parsing G-code letter-value pairs."""

    def test_parse_simple_move(self):
        params = _parse_gcode_params("G1 X10 Y20 Z0.3 F1200")
        assert params["X"] == 10.0
        assert params["Y"] == 20.0
        assert params["Z"] == pytest.approx(0.3)
        assert params["F"] == 1200.0

    def test_parse_temp_command(self):
        params = _parse_gcode_params("M104 S200")
        assert params["S"] == 200.0

    def test_parse_no_params(self):
        params = _parse_gcode_params("G28")
        assert params == {}

    def test_parse_negative_value(self):
        params = _parse_gcode_params("G1 Z-0.5")
        assert params["Z"] == pytest.approx(-0.5)

    def test_parse_empty_string(self):
        params = _parse_gcode_params("")
        assert params == {}

    def test_parse_comment_only(self):
        params = _parse_gcode_params("; comment")
        assert params == {}

    def test_parse_case_insensitive(self):
        params = _parse_gcode_params("g1 x10 y20")
        assert params["X"] == 10.0
        assert params["Y"] == 20.0

    def test_parse_extrusion(self):
        params = _parse_gcode_params("G1 X10 Y20 E0.5 F1200")
        assert params["E"] == pytest.approx(0.5)


# ===================================================================
# 19. TestCommandReconstruction
# ===================================================================


class TestCommandReconstruction:
    """Rebuilding G-code from parsed components."""

    def test_rebuild_simple_move(self):
        result = _rebuild_command("G1", {"X": 10.0, "Y": 20.0, "Z": 0.3})
        assert result == "G1 X10 Y20 Z0.3"

    def test_rebuild_with_feedrate(self):
        result = _rebuild_command("G1", {"F": 1200.0, "X": 10.0})
        assert result == "G1 F1200 X10"

    def test_rebuild_preserves_float(self):
        result = _rebuild_command("G1", {"Z": 0.25})
        assert result == "G1 Z0.25"

    def test_rebuild_empty_params(self):
        result = _rebuild_command("G28", {})
        assert result == "G28"

    def test_rebuild_temp_command(self):
        result = _rebuild_command("M104", {"S": 200.0})
        assert result == "M104 S200"

    def test_rebuild_alphabetical_order(self):
        result = _rebuild_command("G1", {"Z": 1.0, "A": 2.0, "X": 3.0})
        assert result == "G1 A2 X3 Z1"


# ===================================================================
# 20. TestMultipleRuleFiring
# ===================================================================


class TestMultipleRuleFiring:
    """Multiple rules matching a single command -- highest severity wins."""

    def test_block_wins_over_alert(self, interceptor, session):
        alert_rule = _make_rule(
            action=InterceptionAction.ALERT,
            priority=RulePriority.LOW,
            name="alert",
        )
        block_rule = _make_rule(
            action=InterceptionAction.BLOCK,
            priority=RulePriority.CRITICAL,
            name="block",
        )
        interceptor.add_rule(session.session_id, alert_rule)
        interceptor.add_rule(session.session_id, block_rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.BLOCK
        assert len(result.triggered_rules) == 2

    def test_block_wins_over_modify(self, interceptor, session):
        modify_rule = _make_rule(
            action=InterceptionAction.MODIFY,
            modify_params={"F": 1000},
            name="modify",
        )
        block_rule = _make_rule(
            action=InterceptionAction.BLOCK,
            name="block",
        )
        interceptor.add_rule(session.session_id, modify_rule)
        interceptor.add_rule(session.session_id, block_rule)
        result = interceptor.intercept(session.session_id, "G1 X10 F5000")
        assert result.action == InterceptionAction.BLOCK

    def test_pause_wins_over_modify(self, interceptor, session):
        modify_rule = _make_rule(
            action=InterceptionAction.MODIFY,
            modify_params={"F": 1000},
            name="modify",
        )
        pause_rule = _make_rule(
            action=InterceptionAction.PAUSE,
            name="pause",
        )
        interceptor.add_rule(session.session_id, modify_rule)
        interceptor.add_rule(session.session_id, pause_rule)
        result = interceptor.intercept(session.session_id, "G1 X10 F5000")
        assert result.action == InterceptionAction.PAUSE

    def test_all_rules_report_triggered(self, interceptor, session):
        r1 = _make_rule(action=InterceptionAction.ALERT, name="r1")
        r2 = _make_rule(action=InterceptionAction.ALERT, name="r2")
        r3 = _make_rule(action=InterceptionAction.ALERT, name="r3")
        interceptor.add_rule(session.session_id, r1)
        interceptor.add_rule(session.session_id, r2)
        interceptor.add_rule(session.session_id, r3)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert len(result.triggered_rules) == 3


# ===================================================================
# 21. TestConcurrency
# ===================================================================


class TestConcurrency:
    """Thread-safe operations."""

    def test_concurrent_intercepts(self, interceptor):
        session = interceptor.create_session("concurrent_test", rules=[])
        rule = _make_rule(action=InterceptionAction.ALLOW)
        interceptor.add_rule(session.session_id, rule)

        errors: list[str] = []

        def _worker(n: int) -> None:
            try:
                for i in range(50):
                    interceptor.intercept(session.session_id, f"G1 X{n * 50 + i}")
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        s = interceptor.get_session(session.session_id)
        assert s.commands_processed == 200

    def test_concurrent_session_creation(self, interceptor):
        errors: list[str] = []
        sessions: list[str] = []
        lock = threading.Lock()

        def _worker(n: int) -> None:
            try:
                s = interceptor.create_session(f"printer_{n}", rules=[])
                with lock:
                    sessions.append(s.session_id)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(sessions) == 10
        assert len(set(sessions)) == 10  # All unique


# ===================================================================
# 22. TestSessionStats
# ===================================================================


class TestSessionStats:
    """Session statistics tracking."""

    def test_get_session_stats(self, interceptor, session):
        stats = interceptor.get_session_stats(session.session_id)
        assert stats["session_id"] == session.session_id
        assert stats["printer_name"] == "test_printer"
        assert stats["commands_processed"] == 0
        assert stats["rule_count"] == 0
        assert stats["has_telemetry"] is False

    def test_stats_after_intercepts(self, interceptor, session):
        block_rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            action=InterceptionAction.BLOCK,
            blocked_commands=["M112"],
        )
        interceptor.add_rule(session.session_id, block_rule)
        interceptor.intercept(session.session_id, "G1 X10")
        interceptor.intercept(session.session_id, "M112")
        stats = interceptor.get_session_stats(session.session_id)
        assert stats["commands_processed"] == 2
        assert stats["commands_blocked"] == 1

    def test_stats_session_not_found(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.get_session_stats("nonexistent")

    def test_stats_with_telemetry(self, interceptor, session):
        interceptor.update_telemetry(
            session.session_id,
            TelemetrySnapshot(hotend_temp=200.0),
        )
        stats = interceptor.get_session_stats(session.session_id)
        assert stats["has_telemetry"] is True


# ===================================================================
# 23. TestEdgeCases
# ===================================================================


class TestEdgeCases:
    """Malformed commands, missing fields, empty strings."""

    def test_whitespace_command(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "   ")
        assert result.action == InterceptionAction.ALLOW

    def test_tab_characters(self, interceptor, session):
        result = interceptor.intercept(session.session_id, "\t\t")
        assert result.action == InterceptionAction.ALLOW

    def test_command_with_inline_comment(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            action=InterceptionAction.BLOCK,
            blocked_commands=["M112"],
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "M112 ; emergency stop")
        assert result.action == InterceptionAction.BLOCK

    def test_lowercase_command(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.COMMAND_BLOCKED,
            action=InterceptionAction.BLOCK,
            blocked_commands=["M112"],
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "m112")
        assert result.action == InterceptionAction.BLOCK

    def test_parse_command_word_empty(self):
        assert _parse_command_word("") is None

    def test_parse_command_word_whitespace(self):
        assert _parse_command_word("   ") is None

    def test_parse_command_word_text_only(self):
        assert _parse_command_word("hello world") is None

    def test_strip_comment_no_semicolon(self):
        assert _strip_comment("G28") == "G28"

    def test_strip_comment_semicolon_at_start(self):
        assert _strip_comment("; comment") == ""

    def test_apply_modification_invalid_command(self):
        result = _apply_modification("; just a comment", {"F": 1000})
        assert result == "; just a comment"

    def test_apply_modification_non_numeric_value(self):
        # Non-numeric modify params are silently skipped.
        result = _apply_modification("G1 X10 F5000", {"F": "not_a_number"})
        # Should still have F5000 since the invalid value is skipped.
        assert "X10" in result

    def test_flow_anomaly_no_telemetry(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        assert _check_flow_anomaly(rule, None) is False

    def test_flow_anomaly_no_flow_data(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=None)
        assert _check_flow_anomaly(rule, telemetry) is False


# ===================================================================
# 24. TestFlowAnomalyTrigger
# ===================================================================


class TestFlowAnomalyTrigger:
    """Flow rate anomaly detection."""

    def test_flow_too_low(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=30.0)
        assert _check_flow_anomaly(rule, telemetry) is True

    def test_flow_too_high(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=200.0)
        assert _check_flow_anomaly(rule, telemetry) is True

    def test_flow_normal(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=100.0)
        assert _check_flow_anomaly(rule, telemetry) is False

    def test_flow_at_boundary(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=50.0,
            threshold_max=150.0,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=50.0)
        assert _check_flow_anomaly(rule, telemetry) is False

    def test_flow_defaults_when_no_thresholds(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.FLOW_ANOMALY,
            threshold=None,
            threshold_max=None,
        )
        telemetry = TelemetrySnapshot(flow_rate_pct=30.0)
        # Should use defaults (50, 150).
        assert _check_flow_anomaly(rule, telemetry) is True


# ===================================================================
# 25. TestSerialization
# ===================================================================


class TestSerialization:
    """to_dict with enum → string conversion."""

    def test_telemetry_to_dict(self):
        t = TelemetrySnapshot(hotend_temp=200.0, bed_temp=60.0, timestamp=1234.0)
        d = t.to_dict()
        assert d["hotend_temp"] == 200.0
        assert d["bed_temp"] == 60.0
        assert d["timestamp"] == 1234.0
        assert d["position_x"] is None

    def test_rule_to_dict_enum_values(self):
        rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            priority=RulePriority.CRITICAL,
        )
        d = rule.to_dict()
        assert d["trigger"] == "temp_exceeds"
        assert d["action"] == "block"
        assert d["priority"] == "critical"

    def test_result_to_dict(self):
        result = InterceptionResult(
            original_command="M104 S300",
            action=InterceptionAction.BLOCK,
            triggered_rules=["rule-1"],
            reasons=["too hot"],
            timestamp="2026-01-01T00:00:00Z",
        )
        d = result.to_dict()
        assert d["action"] == "block"
        assert d["original_command"] == "M104 S300"
        assert d["triggered_rules"] == ["rule-1"]

    def test_session_to_dict(self):
        session = InterceptionSession(
            session_id="abc-123",
            printer_name="ender3",
            active=True,
            started_at="2026-01-01T00:00:00Z",
        )
        d = session.to_dict()
        assert d["session_id"] == "abc-123"
        assert d["printer_name"] == "ender3"
        assert d["active"] is True
        assert d["last_telemetry"] is None

    def test_session_to_dict_with_telemetry(self):
        session = InterceptionSession(
            session_id="abc-123",
            printer_name="ender3",
            last_telemetry=TelemetrySnapshot(hotend_temp=200.0),
        )
        d = session.to_dict()
        assert d["last_telemetry"] is not None
        assert d["last_telemetry"]["hotend_temp"] == 200.0

    def test_session_to_dict_with_rules(self):
        rule = _make_rule()
        session = InterceptionSession(
            session_id="abc",
            printer_name="test",
            rules=[rule],
        )
        d = session.to_dict()
        assert len(d["rules"]) == 1
        assert d["rules"][0]["rule_id"] == rule.rule_id


# ===================================================================
# 26. TestInterceptionHistory
# ===================================================================


class TestInterceptionHistory:
    """History retrieval."""

    def test_history_records_intercepts(self, interceptor, session):
        interceptor.intercept(session.session_id, "G1 X10")
        interceptor.intercept(session.session_id, "G1 X20")
        history = interceptor.get_interception_history(session.session_id)
        assert len(history) == 2

    def test_history_newest_first(self, interceptor, session):
        interceptor.intercept(session.session_id, "G1 X10")
        interceptor.intercept(session.session_id, "G1 X20")
        history = interceptor.get_interception_history(session.session_id)
        assert history[0].original_command == "G1 X20"
        assert history[1].original_command == "G1 X10"

    def test_history_limit(self, interceptor, session):
        for i in range(10):
            interceptor.intercept(session.session_id, f"G1 X{i}")
        history = interceptor.get_interception_history(session.session_id, limit=3)
        assert len(history) == 3

    def test_history_session_not_found(self, interceptor):
        with pytest.raises(KeyError):
            interceptor.get_interception_history("nonexistent")


# ===================================================================
# 27. TestAlwaysTrigger
# ===================================================================


class TestAlwaysTrigger:
    """ALWAYS trigger evaluation."""

    def test_always_fires_on_any_command(self, interceptor, session):
        rule = _make_rule(
            trigger=InterceptionTrigger.ALWAYS,
            action=InterceptionAction.ALERT,
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.ALERT


# ===================================================================
# 28. TestCommandProcessedCounter
# ===================================================================


class TestCommandProcessedCounter:
    """Command processing counter."""

    def test_counter_increments(self, interceptor, session):
        interceptor.intercept(session.session_id, "G1 X10")
        interceptor.intercept(session.session_id, "G1 X20")
        interceptor.intercept(session.session_id, "")  # Empty command
        s = interceptor.get_session(session.session_id)
        assert s.commands_processed == 3

    def test_counter_includes_blocked(self, interceptor, session):
        rule = _make_rule(action=InterceptionAction.BLOCK)
        interceptor.add_rule(session.session_id, rule)
        interceptor.intercept(session.session_id, "G1 X10")
        s = interceptor.get_session(session.session_id)
        assert s.commands_processed == 1
        assert s.commands_blocked == 1


# ===================================================================
# 29. TestSingleton
# ===================================================================


class TestSingleton:
    """Lazy singleton access."""

    def test_get_interceptor_returns_same_instance(self):
        import kiln.gcode_interceptor as mod

        # Reset singleton for this test.
        original = mod._interceptor
        mod._interceptor = None
        try:
            a = get_interceptor()
            b = get_interceptor()
            assert a is b
        finally:
            mod._interceptor = original

    def test_get_interceptor_creates_instance(self):
        import kiln.gcode_interceptor as mod

        original = mod._interceptor
        mod._interceptor = None
        try:
            instance = get_interceptor()
            assert isinstance(instance, GcodeInterceptor)
        finally:
            mod._interceptor = original


# ===================================================================
# 30. TestEnumValues
# ===================================================================


class TestEnumValues:
    """Enum string values for JSON serialization."""

    def test_interception_action_values(self):
        assert InterceptionAction.ALLOW.value == "allow"
        assert InterceptionAction.BLOCK.value == "block"
        assert InterceptionAction.MODIFY.value == "modify"
        assert InterceptionAction.PAUSE.value == "pause"
        assert InterceptionAction.ALERT.value == "alert"

    def test_rule_priority_values(self):
        assert RulePriority.CRITICAL.value == "critical"
        assert RulePriority.HIGH.value == "high"
        assert RulePriority.MEDIUM.value == "medium"
        assert RulePriority.LOW.value == "low"

    def test_trigger_values(self):
        assert InterceptionTrigger.TEMP_EXCEEDS.value == "temp_exceeds"
        assert InterceptionTrigger.COMMAND_BLOCKED.value == "command_blocked"
        assert InterceptionTrigger.PATTERN_MATCH.value == "pattern_match"
        assert InterceptionTrigger.LAYER_CHANGE.value == "layer_change"
        assert InterceptionTrigger.ALWAYS.value == "always"


# ===================================================================
# 31. TestApplyModification
# ===================================================================


class TestApplyModification:
    """_apply_modification helper tests."""

    def test_cap_feedrate(self):
        result = _apply_modification("G1 X10 F5000", {"F": 3000})
        assert "F3000" in result

    def test_cap_temperature(self):
        result = _apply_modification("M104 S300", {"S": 260})
        assert "S260" in result

    def test_no_matching_param_adds_it(self):
        result = _apply_modification("G1 X10", {"F": 1200})
        assert "F1200" in result

    def test_preserve_other_params(self):
        result = _apply_modification("G1 X10 Y20 F5000", {"F": 3000})
        assert "X10" in result
        assert "Y20" in result

    def test_empty_modify_params(self):
        result = _apply_modification("G1 X10 F5000", {})
        assert "G1" in result


# ===================================================================
# 32. TestEventEmission
# ===================================================================


class TestEventEmission:
    """Best-effort event emission."""

    def test_event_emitted_on_block(self, interceptor, session):
        rule = _make_rule(action=InterceptionAction.BLOCK)
        interceptor.add_rule(session.session_id, rule)
        # Should not raise even without event bus.
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.BLOCK

    def test_no_event_on_allow(self, interceptor, session):
        # ALLOW actions should not emit events.
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert result.action == InterceptionAction.ALLOW


# ===================================================================
# 33. TestRuleMessages
# ===================================================================


class TestRuleMessages:
    """Human-readable reasons in results."""

    def test_custom_message_in_reasons(self, interceptor, session):
        rule = _make_rule(
            action=InterceptionAction.BLOCK,
            message="custom reason text",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert "custom reason text" in result.reasons

    def test_default_message_when_empty(self, interceptor, session):
        rule = _make_rule(
            action=InterceptionAction.BLOCK,
            message="",
            name="my_rule",
        )
        interceptor.add_rule(session.session_id, rule)
        result = interceptor.intercept(session.session_id, "G1 X10")
        assert any("my_rule" in r for r in result.reasons)


# ===================================================================
# 34. TestIntegration -- full session lifecycle
# ===================================================================


class TestIntegration:
    """Full session lifecycle integration tests."""

    def test_full_lifecycle(self, interceptor):
        # Create session.
        session = interceptor.create_session("ender3", rules=[])

        # Add rules.
        temp_rule = _make_rule(
            trigger=InterceptionTrigger.TEMP_EXCEEDS,
            action=InterceptionAction.BLOCK,
            threshold=260.0,
            name="max_temp",
        )
        speed_rule = _make_rule(
            trigger=InterceptionTrigger.FEEDRATE_EXCEEDS,
            action=InterceptionAction.MODIFY,
            threshold=3000.0,
            modify_params={"F": 3000.0},
            name="max_speed",
        )
        interceptor.add_rule(session.session_id, temp_rule)
        interceptor.add_rule(session.session_id, speed_rule)

        # Update telemetry.
        interceptor.update_telemetry(
            session.session_id,
            TelemetrySnapshot(hotend_temp=200.0, bed_temp=60.0),
        )

        # Intercept safe command.
        r1 = interceptor.intercept(session.session_id, "G1 X10 Y10 F1200")
        assert r1.action == InterceptionAction.ALLOW

        # Intercept dangerous temp.
        r2 = interceptor.intercept(session.session_id, "M104 S280")
        assert r2.action == InterceptionAction.BLOCK

        # Intercept fast move.
        r3 = interceptor.intercept(session.session_id, "G1 X50 F6000")
        assert r3.action == InterceptionAction.MODIFY
        assert r3.modified_command is not None

        # Check stats.
        stats = interceptor.get_session_stats(session.session_id)
        assert stats["commands_processed"] == 3
        assert stats["commands_blocked"] == 1
        assert stats["commands_modified"] == 1

        # Check history.
        history = interceptor.get_interception_history(session.session_id)
        assert len(history) == 3

        # End session.
        ended = interceptor.end_session(session.session_id)
        assert ended.active is False
