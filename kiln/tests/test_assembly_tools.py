"""Tests for assembly_tools.py plugin MCP tools.

Covers the MCP tool wrappers (underlying assembly module tested in
test_assembly.py):
- add_assembly_part — happy path, invalid JSON
- add_assembly_interface — happy path, invalid JSON
- check_assembly_clearances — happy path, invalid JSON
- compose_assembly_parts — happy path, invalid JSON
- get_joint_recommendation — happy path, error
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_assembly_tools() -> dict:
    """Register the plugin on a mock MCP and return captured tool functions."""
    from kiln.plugins.assembly_tools import plugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    plugin.register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def assembly_tools():
    return _register_assembly_tools()


def _assembly_json(name: str = "test_asm") -> str:
    return json.dumps({
        "name": name,
        "assembly_id": "asm-1",
        "parts": [],
        "interfaces": [],
        "validation_results": None,
    })


# ---------------------------------------------------------------------------
# TestAddAssemblyPart
# ---------------------------------------------------------------------------


class TestAddAssemblyPart:
    """Tests for add_assembly_part MCP tool."""

    @patch("kiln.assembly.AssemblyPart")
    @patch("kiln.assembly.Assembly.from_dict")
    def test_happy_path(self, mock_from_dict, mock_part_cls, assembly_tools):
        mock_asm = MagicMock()
        mock_asm.parts = []
        mock_asm.to_dict.return_value = {"name": "test", "parts": [{"part_id": "p1"}]}
        mock_from_dict.return_value = mock_asm
        mock_part_cls.return_value = MagicMock()

        result = assembly_tools["add_assembly_part"](
            assembly_json=_assembly_json(),
            part_id="p1",
            file_path="/tmp/part.stl",
        )

        assert result["success"] is True
        assert "data" in result

    def test_invalid_json(self, assembly_tools):
        result = assembly_tools["add_assembly_part"](
            assembly_json="not json",
            part_id="p1",
            file_path="/tmp/part.stl",
        )

        assert result["success"] is False
        assert "Invalid assembly JSON" in result["error"]


# ---------------------------------------------------------------------------
# TestAddAssemblyInterface
# ---------------------------------------------------------------------------


class TestAddAssemblyInterface:
    """Tests for add_assembly_interface MCP tool."""

    @patch("kiln.assembly.MatingInterface")
    @patch("kiln.assembly.Assembly.from_dict")
    def test_happy_path(self, mock_from_dict, mock_iface_cls, assembly_tools):
        mock_asm = MagicMock()
        mock_asm.interfaces = []
        mock_asm.to_dict.return_value = {"name": "test", "interfaces": [{"part_a": "p1"}]}
        mock_from_dict.return_value = mock_asm
        mock_iface_cls.return_value = MagicMock()

        result = assembly_tools["add_assembly_interface"](
            assembly_json=_assembly_json(),
            part_a_id="p1",
            part_b_id="p2",
        )

        assert result["success"] is True

    def test_invalid_json(self, assembly_tools):
        result = assembly_tools["add_assembly_interface"](
            assembly_json="{{bad",
            part_a_id="p1",
            part_b_id="p2",
        )

        assert result["success"] is False
        assert "Invalid assembly JSON" in result["error"]


# ---------------------------------------------------------------------------
# TestCheckAssemblyClearances
# ---------------------------------------------------------------------------


class TestCheckAssemblyClearances:
    """Tests for check_assembly_clearances MCP tool."""

    @patch("kiln.assembly.check_all_clearances")
    @patch("kiln.assembly.Assembly.from_dict")
    def test_happy_path(self, mock_from_dict, mock_check, assembly_tools):
        mock_from_dict.return_value = MagicMock()
        check = MagicMock()
        check.to_dict.return_value = {"pass": True, "clearance_mm": 0.2}
        mock_check.return_value = [check]

        result = assembly_tools["check_assembly_clearances"](
            assembly_json=_assembly_json(),
        )

        assert result["success"] is True
        assert len(result["data"]) == 1

    def test_invalid_json(self, assembly_tools):
        result = assembly_tools["check_assembly_clearances"](
            assembly_json="not json",
        )

        assert result["success"] is False
        assert "Invalid assembly JSON" in result["error"]


# ---------------------------------------------------------------------------
# TestComposeAssemblyParts
# ---------------------------------------------------------------------------


class TestComposeAssemblyParts:
    """Tests for compose_assembly_parts MCP tool."""

    @patch("kiln.assembly.compose_assembly")
    @patch("kiln.assembly.Assembly.from_dict")
    def test_happy_path(self, mock_from_dict, mock_compose, assembly_tools):
        mock_from_dict.return_value = MagicMock()
        mock_compose.return_value = {"output_path": "/tmp/out.stl", "parts_count": 3}

        result = assembly_tools["compose_assembly_parts"](
            assembly_json=_assembly_json(),
            output_path="/tmp/out.stl",
        )

        assert result["success"] is True
        assert result["data"]["output_path"] == "/tmp/out.stl"

    def test_invalid_json(self, assembly_tools):
        result = assembly_tools["compose_assembly_parts"](
            assembly_json="not json",
            output_path="/tmp/out.stl",
        )

        assert result["success"] is False
        assert "Invalid assembly JSON" in result["error"]


# ---------------------------------------------------------------------------
# TestGetJointRecommendation
# ---------------------------------------------------------------------------


class TestGetJointRecommendation:
    """Tests for get_joint_recommendation MCP tool."""

    @patch("kiln.assembly.get_clearance_recommendation")
    def test_happy_path(self, mock_rec, assembly_tools):
        mock_rec.return_value = {
            "joint_type": "clearance_fit",
            "recommended_clearance_mm": 0.2,
        }

        result = assembly_tools["get_joint_recommendation"](
            joint_type="clearance_fit",
        )

        assert result["success"] is True
        assert result["data"]["recommended_clearance_mm"] == 0.2

    @patch("kiln.assembly.get_clearance_recommendation")
    def test_error(self, mock_rec, assembly_tools):
        mock_rec.side_effect = RuntimeError("unknown joint")

        result = assembly_tools["get_joint_recommendation"](
            joint_type="unknown",
        )

        assert result["success"] is False
