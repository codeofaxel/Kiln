"""Tests for the get_started() onboarding MCP tool.

Covers:
- Return structure contains all expected keys
- safety_tools list includes safety_status
- session_recovery section exists with correct fields
- tip references safety_status
"""

from __future__ import annotations

from kiln.server import get_started


class TestGetStarted:
    """Tests for the get_started() MCP tool."""

    def test_returns_success(self):
        result = get_started()
        assert result["success"] is True

    def test_has_required_keys(self):
        result = get_started()
        expected_keys = {
            "success",
            "overview",
            "quick_start",
            "core_workflows",
            "safety_tools",
            "tool_tiers",
            "session_recovery",
            "tip",
        }
        assert expected_keys == set(result.keys())

    def test_safety_tools_includes_safety_status(self):
        result = get_started()
        safety_tools = result["safety_tools"]
        status_entries = [t for t in safety_tools if t.startswith("safety_status")]
        assert len(status_entries) == 1
        assert "comprehensive safety dashboard" in status_entries[0]

    def test_safety_tools_still_includes_safety_settings(self):
        result = get_started()
        safety_tools = result["safety_tools"]
        settings_entries = [t for t in safety_tools if t.startswith("safety_settings")]
        assert len(settings_entries) == 1

    def test_safety_tools_order(self):
        """safety_status should appear before safety_settings."""
        result = get_started()
        tools = result["safety_tools"]
        status_idx = next(i for i, t in enumerate(tools) if "safety_status" in t)
        settings_idx = next(i for i, t in enumerate(tools) if "safety_settings" in t)
        assert status_idx < settings_idx

    def test_session_recovery_structure(self):
        result = get_started()
        sr = result["session_recovery"]
        assert "description" in sr
        assert sr["tool"] == "get_agent_context"
        assert "usage" in sr
        assert "get_agent_context" in sr["usage"]

    def test_tip_mentions_safety_status(self):
        result = get_started()
        assert "safety_status" in result["tip"]

    def test_tip_mentions_safety_settings(self):
        result = get_started()
        assert "safety_settings" in result["tip"]
