"""Tests for G-code interception tool plugin.

Covers:
- start_gcode_interception() — happy path, ValueError, unexpected error
- stop_gcode_interception() — happy path, missing session, unexpected error
- add_interception_rule() — happy path, invalid trigger/action/priority, session error
- remove_interception_rule() — found, not found, missing session
- intercept_gcode_command() — happy path, missing session, unexpected error
- update_interception_telemetry() — happy path, missing session, unexpected error
- get_interception_status() — happy path, missing session
- list_interception_sessions() — empty, populated, unexpected error
- load_safety_interception_rules() — happy path, missing session, unknown printer
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session(
    *,
    session_id: str = "sess-123",
    printer: str = "ender3",
    rules: int = 5,
    commands: int = 42,
) -> MagicMock:
    """Build a mock InterceptionSession."""
    sess = MagicMock()
    sess.session_id = session_id
    sess.printer_name = printer
    sess.rules = [MagicMock() for _ in range(rules)]
    sess.commands_processed = commands
    sess.to_dict.return_value = {
        "session_id": session_id,
        "printer_name": printer,
        "rules_count": rules,
        "commands_processed": commands,
    }
    return sess


def _mock_rule(*, rule_id: str = "rule-1", name: str = "temp_cap") -> MagicMock:
    rule = MagicMock()
    rule.rule_id = rule_id
    rule.name = name
    rule.to_dict.return_value = {"rule_id": rule_id, "name": name}
    return rule


def _mock_intercept_result(action: str = "allow") -> MagicMock:
    result = MagicMock()
    result.action = action
    result.to_dict.return_value = {"action": action, "triggered_rules": []}
    return result


# ---------------------------------------------------------------------------
# TestStartGcodeInterception
# ---------------------------------------------------------------------------


class TestStartGcodeInterception:
    """Tests for start_gcode_interception()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import start_gcode_interception

        mock_interceptor = MagicMock()
        mock_interceptor.create_session.return_value = _mock_session()
        mock_get.return_value = mock_interceptor

        result = start_gcode_interception("ender3")

        assert result["success"] is True
        assert "session" in result
        assert "5 safety rules" in result["message"]
        mock_interceptor.create_session.assert_called_once_with("ender3")

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_value_error(self, mock_get):
        from kiln.plugins.interception_tools import start_gcode_interception

        mock_interceptor = MagicMock()
        mock_interceptor.create_session.side_effect = ValueError("bad printer")
        mock_get.return_value = mock_interceptor

        result = start_gcode_interception("bogus")

        assert result["success"] is False
        assert "bad printer" in result["error"]

    @patch("kiln.gcode_interceptor.get_interceptor", side_effect=RuntimeError("boom"))
    def test_unexpected_error(self, mock_get):
        from kiln.plugins.interception_tools import start_gcode_interception

        result = start_gcode_interception("ender3")

        assert result["success"] is False
        assert "Unexpected" in result["error"]


# ---------------------------------------------------------------------------
# TestStopGcodeInterception
# ---------------------------------------------------------------------------


class TestStopGcodeInterception:
    """Tests for stop_gcode_interception()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import stop_gcode_interception

        mock_interceptor = MagicMock()
        mock_interceptor.end_session.return_value = _mock_session(commands=10)
        mock_get.return_value = mock_interceptor

        result = stop_gcode_interception("sess-123")

        assert result["success"] is True
        assert "10 commands" in result["message"]

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_missing_session(self, mock_get):
        from kiln.plugins.interception_tools import stop_gcode_interception

        mock_interceptor = MagicMock()
        mock_interceptor.end_session.side_effect = KeyError("not found")
        mock_get.return_value = mock_interceptor

        result = stop_gcode_interception("bad-id")

        assert result["success"] is False

    @patch("kiln.gcode_interceptor.get_interceptor", side_effect=RuntimeError("boom"))
    def test_unexpected_error(self, mock_get):
        from kiln.plugins.interception_tools import stop_gcode_interception

        result = stop_gcode_interception("sess-123")

        assert result["success"] is False
        assert "Unexpected" in result["error"]


# ---------------------------------------------------------------------------
# TestAddInterceptionRule
# ---------------------------------------------------------------------------


class TestAddInterceptionRule:
    """Tests for add_interception_rule()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import add_interception_rule

        mock_interceptor = MagicMock()
        added_rule = _mock_rule()
        mock_interceptor.add_rule.return_value = added_rule
        mock_get.return_value = mock_interceptor

        result = add_interception_rule(
            session_id="sess-1",
            name="temp_cap",
            trigger="temp_exceeds",
            action="block",
            priority="high",
            threshold=280.0,
        )

        assert result["success"] is True
        assert result["rule"]["name"] == "temp_cap"

    def test_invalid_trigger(self):
        from kiln.plugins.interception_tools import add_interception_rule

        result = add_interception_rule(
            session_id="sess-1",
            name="bad",
            trigger="nonexistent_trigger",
            action="block",
        )

        assert result["success"] is False
        assert "Invalid trigger" in result["error"]

    @patch("kiln.gcode_interceptor.InterceptionTrigger", side_effect=lambda x: x)
    def test_invalid_action(self, _trigger):
        from kiln.plugins.interception_tools import add_interception_rule

        result = add_interception_rule(
            session_id="sess-1",
            name="bad",
            trigger="temp_exceeds",
            action="explode",
        )

        assert result["success"] is False
        assert "Invalid action" in result["error"]

    @patch("kiln.gcode_interceptor.InterceptionAction", side_effect=lambda x: x)
    @patch("kiln.gcode_interceptor.InterceptionTrigger", side_effect=lambda x: x)
    def test_invalid_priority(self, _trigger, _action):
        from kiln.plugins.interception_tools import add_interception_rule

        result = add_interception_rule(
            session_id="sess-1",
            name="bad",
            trigger="temp_exceeds",
            action="block",
            priority="ultra",
        )

        assert result["success"] is False
        assert "Invalid priority" in result["error"]


# ---------------------------------------------------------------------------
# TestRemoveInterceptionRule
# ---------------------------------------------------------------------------


class TestRemoveInterceptionRule:
    """Tests for remove_interception_rule()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_found(self, mock_get):
        from kiln.plugins.interception_tools import remove_interception_rule

        mock_interceptor = MagicMock()
        mock_interceptor.remove_rule.return_value = True
        mock_get.return_value = mock_interceptor

        result = remove_interception_rule("sess-1", "rule-1")

        assert result["success"] is True
        assert "removed" in result["message"].lower()

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_not_found(self, mock_get):
        from kiln.plugins.interception_tools import remove_interception_rule

        mock_interceptor = MagicMock()
        mock_interceptor.remove_rule.return_value = False
        mock_get.return_value = mock_interceptor

        result = remove_interception_rule("sess-1", "rule-999")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_missing_session(self, mock_get):
        from kiln.plugins.interception_tools import remove_interception_rule

        mock_interceptor = MagicMock()
        mock_interceptor.remove_rule.side_effect = KeyError("no session")
        mock_get.return_value = mock_interceptor

        result = remove_interception_rule("bad-sess", "rule-1")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestInterceptGcodeCommand
# ---------------------------------------------------------------------------


class TestInterceptGcodeCommand:
    """Tests for intercept_gcode_command()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import intercept_gcode_command

        mock_interceptor = MagicMock()
        mock_interceptor.intercept.return_value = _mock_intercept_result("allow")
        mock_get.return_value = mock_interceptor

        result = intercept_gcode_command("sess-1", "G1 X10 F3000")

        assert result["success"] is True
        assert result["result"]["action"] == "allow"

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_missing_session(self, mock_get):
        from kiln.plugins.interception_tools import intercept_gcode_command

        mock_interceptor = MagicMock()
        mock_interceptor.intercept.side_effect = KeyError("no session")
        mock_get.return_value = mock_interceptor

        result = intercept_gcode_command("bad", "G1")

        assert result["success"] is False

    @patch("kiln.gcode_interceptor.get_interceptor", side_effect=RuntimeError("boom"))
    def test_unexpected_error(self, mock_get):
        from kiln.plugins.interception_tools import intercept_gcode_command

        result = intercept_gcode_command("sess-1", "G28")

        assert result["success"] is False
        assert "Unexpected" in result["error"]


# ---------------------------------------------------------------------------
# TestUpdateInterceptionTelemetry
# ---------------------------------------------------------------------------


class TestUpdateInterceptionTelemetry:
    """Tests for update_interception_telemetry()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    @patch("kiln.gcode_interceptor.TelemetrySnapshot")
    def test_happy_path(self, mock_snap_cls, mock_get):
        from kiln.plugins.interception_tools import update_interception_telemetry

        mock_snap = MagicMock()
        mock_snap.to_dict.return_value = {"hotend_temp": 200.0}
        mock_snap_cls.return_value = mock_snap

        mock_interceptor = MagicMock()
        mock_get.return_value = mock_interceptor

        result = update_interception_telemetry("sess-1", hotend_temp=200.0)

        assert result["success"] is True
        assert result["telemetry"]["hotend_temp"] == 200.0

    @patch("kiln.gcode_interceptor.get_interceptor")
    @patch("kiln.gcode_interceptor.TelemetrySnapshot")
    def test_missing_session(self, mock_snap_cls, mock_get):
        from kiln.plugins.interception_tools import update_interception_telemetry

        mock_snap_cls.return_value = MagicMock()
        mock_interceptor = MagicMock()
        mock_interceptor.update_telemetry.side_effect = KeyError("no session")
        mock_get.return_value = mock_interceptor

        result = update_interception_telemetry("bad-sess")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestGetInterceptionStatus
# ---------------------------------------------------------------------------


class TestGetInterceptionStatus:
    """Tests for get_interception_status()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import get_interception_status

        mock_interceptor = MagicMock()
        mock_interceptor.get_session_stats.return_value = {"commands": 42}
        mock_get.return_value = mock_interceptor

        result = get_interception_status("sess-1")

        assert result["success"] is True
        assert result["stats"]["commands"] == 42

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_missing_session(self, mock_get):
        from kiln.plugins.interception_tools import get_interception_status

        mock_interceptor = MagicMock()
        mock_interceptor.get_session_stats.side_effect = KeyError("no session")
        mock_get.return_value = mock_interceptor

        result = get_interception_status("bad-sess")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestListInterceptionSessions
# ---------------------------------------------------------------------------


class TestListInterceptionSessions:
    """Tests for list_interception_sessions()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_empty(self, mock_get):
        from kiln.plugins.interception_tools import list_interception_sessions

        mock_interceptor = MagicMock()
        mock_interceptor.get_active_sessions.return_value = []
        mock_get.return_value = mock_interceptor

        result = list_interception_sessions()

        assert result["success"] is True
        assert result["count"] == 0
        assert result["sessions"] == []

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_populated(self, mock_get):
        from kiln.plugins.interception_tools import list_interception_sessions

        mock_interceptor = MagicMock()
        mock_interceptor.get_active_sessions.return_value = [
            _mock_session(session_id="s1"),
            _mock_session(session_id="s2"),
        ]
        mock_get.return_value = mock_interceptor

        result = list_interception_sessions()

        assert result["success"] is True
        assert result["count"] == 2

    @patch("kiln.gcode_interceptor.get_interceptor", side_effect=RuntimeError("boom"))
    def test_unexpected_error(self, mock_get):
        from kiln.plugins.interception_tools import list_interception_sessions

        result = list_interception_sessions()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestLoadSafetyInterceptionRules
# ---------------------------------------------------------------------------


class TestLoadSafetyInterceptionRules:
    """Tests for load_safety_interception_rules()."""

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_happy_path(self, mock_get):
        from kiln.plugins.interception_tools import load_safety_interception_rules

        rules = [_mock_rule(rule_id=f"r{i}") for i in range(3)]
        mock_interceptor = MagicMock()
        mock_interceptor.load_safety_rules.return_value = rules
        mock_get.return_value = mock_interceptor

        result = load_safety_interception_rules("sess-1", "ender3")

        assert result["success"] is True
        assert result["rules_added"] == 3
        assert "3 safety rules" in result["message"]

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_missing_session(self, mock_get):
        from kiln.plugins.interception_tools import load_safety_interception_rules

        mock_interceptor = MagicMock()
        mock_interceptor.load_safety_rules.return_value = [_mock_rule()]
        mock_interceptor.add_rule.side_effect = KeyError("no session")
        mock_get.return_value = mock_interceptor

        result = load_safety_interception_rules("bad", "ender3")

        assert result["success"] is False

    @patch("kiln.gcode_interceptor.get_interceptor")
    def test_unknown_printer(self, mock_get):
        from kiln.plugins.interception_tools import load_safety_interception_rules

        mock_interceptor = MagicMock()
        mock_interceptor.load_safety_rules.side_effect = ValueError("unknown model")
        mock_get.return_value = mock_interceptor

        result = load_safety_interception_rules("sess-1", "bogus_printer")

        assert result["success"] is False
