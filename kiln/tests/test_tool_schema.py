"""Tests for kiln.tool_schema -- OpenAI function-calling schema converter."""

from __future__ import annotations

import types
import typing
from typing import Dict, List, Optional, Union
from unittest import mock

import pytest

from kiln.tool_schema import (
    _build_schema_from_function,
    _parse_docstring,
    _python_type_to_json_schema,
)


# ---------------------------------------------------------------------------
# 1. _python_type_to_json_schema
# ---------------------------------------------------------------------------


class TestPythonTypeToJsonSchema:
    """Test the type annotation to JSON Schema converter."""

    def test_str(self):
        schema, optional = _python_type_to_json_schema(str)
        assert schema == {"type": "string"}
        assert optional is False

    def test_int(self):
        schema, optional = _python_type_to_json_schema(int)
        assert schema == {"type": "integer"}
        assert optional is False

    def test_float(self):
        schema, optional = _python_type_to_json_schema(float)
        assert schema == {"type": "number"}
        assert optional is False

    def test_bool(self):
        schema, optional = _python_type_to_json_schema(bool)
        assert schema == {"type": "boolean"}
        assert optional is False

    def test_list(self):
        schema, optional = _python_type_to_json_schema(list)
        assert schema == {"type": "array"}
        assert optional is False

    def test_dict(self):
        schema, optional = _python_type_to_json_schema(dict)
        assert schema == {"type": "object"}
        assert optional is False

    def test_optional_str(self):
        schema, optional = _python_type_to_json_schema(Optional[str])
        assert schema == {"type": "string"}
        assert optional is True

    def test_optional_int(self):
        schema, optional = _python_type_to_json_schema(Optional[int])
        assert schema == {"type": "integer"}
        assert optional is True

    def test_union_str_none(self):
        schema, optional = _python_type_to_json_schema(Union[str, None])
        assert schema == {"type": "string"}
        assert optional is True

    def test_list_str(self):
        schema, optional = _python_type_to_json_schema(List[str])
        assert schema == {"type": "array", "items": {"type": "string"}}
        assert optional is False

    def test_list_int(self):
        schema, optional = _python_type_to_json_schema(List[int])
        assert schema == {"type": "array", "items": {"type": "integer"}}
        assert optional is False

    def test_dict_typed(self):
        schema, optional = _python_type_to_json_schema(Dict[str, int])
        assert schema == {"type": "object"}
        assert optional is False

    def test_unknown_type_falls_back_to_string(self):
        class Custom:
            pass
        schema, optional = _python_type_to_json_schema(Custom)
        assert schema == {"type": "string"}
        assert optional is False

    def test_str_none_pipe_syntax(self):
        """Test str | None (Python 3.10+ union syntax)."""
        annotation = str | None
        # The actual pipe syntax creates types.UnionType; let's test via eval
        # if available (Python 3.10+).
        try:
            ann = eval("str | None")
            schema, optional = _python_type_to_json_schema(ann)
            assert schema == {"type": "string"}
            assert optional is True
        except TypeError:
            pytest.skip("str | None syntax not available in this Python version")


# ---------------------------------------------------------------------------
# 2. _parse_docstring
# ---------------------------------------------------------------------------


class TestParseDocstring:
    def test_none_docstring(self):
        desc, params = _parse_docstring(None)
        assert desc == ""
        assert params == {}

    def test_empty_docstring(self):
        desc, params = _parse_docstring("")
        assert desc == ""
        assert params == {}

    def test_simple_description_only(self):
        doc = "Do something useful."
        desc, params = _parse_docstring(doc)
        assert desc == "Do something useful."
        assert params == {}

    def test_multiline_description(self):
        doc = "First line.\nSecond line."
        desc, params = _parse_docstring(doc)
        assert desc == "First line. Second line."

    def test_description_with_args_section(self):
        doc = """Do a thing.

        Args:
            name: The name to use.
            count: How many times.
        """
        desc, params = _parse_docstring(doc)
        assert desc == "Do a thing."
        assert params["name"] == "The name to use."
        assert params["count"] == "How many times."

    def test_args_with_type_hints(self):
        doc = """Summary.

        Args:
            host (str): The hostname.
            port (int): The port number.
        """
        desc, params = _parse_docstring(doc)
        assert params["host"] == "The hostname."
        assert params["port"] == "The port number."

    def test_multiline_param_description(self):
        doc = """Summary.

        Args:
            name: A very long description
                that continues on the next line.
        """
        desc, params = _parse_docstring(doc)
        assert "very long description" in params["name"]
        assert "continues on the next line" in params["name"]

    def test_stops_at_returns_section(self):
        doc = """Summary.

        Args:
            x: The input.

        Returns:
            The output.
        """
        desc, params = _parse_docstring(doc)
        assert "x" in params
        assert len(params) == 1

    def test_stops_at_raises_section(self):
        doc = """Summary.

        Args:
            x: The input.

        Raises:
            ValueError: If bad.
        """
        desc, params = _parse_docstring(doc)
        assert "x" in params
        assert len(params) == 1

    def test_description_stops_at_blank_line(self):
        doc = """First paragraph.

        More text after blank.
        """
        desc, params = _parse_docstring(doc)
        assert desc == "First paragraph."


# ---------------------------------------------------------------------------
# 3. _build_schema_from_function
# ---------------------------------------------------------------------------


class TestBuildSchemaFromFunction:
    def test_no_params(self):
        def my_tool():
            """A simple tool."""
            pass

        schema = _build_schema_from_function(my_tool)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "my_tool"
        assert schema["function"]["description"] == "A simple tool."
        assert schema["function"]["parameters"]["type"] == "object"
        assert schema["function"]["parameters"]["properties"] == {}

    def test_with_typed_params(self):
        def printer_status(host: str, port: int = 80):
            """Get printer status.

            Args:
                host: Printer hostname.
                port: Port number.
            """
            pass

        schema = _build_schema_from_function(printer_status)
        props = schema["function"]["parameters"]["properties"]
        assert props["host"]["type"] == "string"
        assert props["host"]["description"] == "Printer hostname."
        assert props["port"]["type"] == "integer"
        assert props["port"]["default"] == 80
        assert "host" in schema["function"]["parameters"].get("required", [])
        assert "port" not in schema["function"]["parameters"].get("required", [])

    def test_optional_param(self):
        def my_tool(name: str, tag: Optional[str] = None):
            """A tool."""
            pass

        schema = _build_schema_from_function(my_tool)
        props = schema["function"]["parameters"]["properties"]
        assert props["name"]["type"] == "string"
        assert props["tag"]["type"] == "string"
        assert "name" in schema["function"]["parameters"]["required"]
        assert "tag" not in schema["function"]["parameters"].get("required", [])

    def test_skips_self_and_cls(self):
        def method(self, name: str):
            """A method."""
            pass

        schema = _build_schema_from_function(method)
        props = schema["function"]["parameters"]["properties"]
        assert "self" not in props
        assert "name" in props

    def test_no_docstring(self):
        def bare_tool(x: int):
            pass

        schema = _build_schema_from_function(bare_tool)
        assert schema["function"]["description"] == ""

    def test_list_param(self):
        def multi(items: List[str]):
            """Process items."""
            pass

        schema = _build_schema_from_function(multi)
        props = schema["function"]["parameters"]["properties"]
        assert props["items"]["type"] == "array"
        assert props["items"]["items"] == {"type": "string"}

    def test_unannotated_param_defaults_to_string(self):
        def legacy(name):
            """Legacy tool."""
            pass

        schema = _build_schema_from_function(legacy)
        props = schema["function"]["parameters"]["properties"]
        assert props["name"]["type"] == "string"


# ---------------------------------------------------------------------------
# 4. Module-level functions that need mocking (_ensure_loaded, etc.)
# ---------------------------------------------------------------------------


class TestToolRegistryWithMocks:
    """Test get_all_tool_schemas, get_tool_function, get_tool_registry with mocked state."""

    def _setup_mock_registry(self):
        """Set up the module-level caches with fake data."""
        import kiln.tool_schema as mod

        def fake_status():
            return {"status": "ok"}

        def fake_files():
            return {"files": []}

        mod._loaded = True
        mod._TOOL_REGISTRY = {
            "printer_status": fake_status,
            "printer_files": fake_files,
        }
        mod._SCHEMA_CACHE = {
            "printer_status": {
                "type": "function",
                "function": {
                    "name": "printer_status",
                    "description": "Get printer status",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            "printer_files": {
                "type": "function",
                "function": {
                    "name": "printer_files",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        }
        return mod

    def _teardown_mock_registry(self, mod):
        mod._loaded = False
        mod._TOOL_REGISTRY = {}
        mod._SCHEMA_CACHE = {}

    def test_get_tool_registry_returns_dict(self):
        mod = self._setup_mock_registry()
        try:
            registry = mod.get_tool_registry()
            assert isinstance(registry, dict)
            assert "printer_status" in registry
            assert "printer_files" in registry
            assert len(registry) == 2
        finally:
            self._teardown_mock_registry(mod)

    def test_get_tool_registry_returns_copy(self):
        mod = self._setup_mock_registry()
        try:
            r1 = mod.get_tool_registry()
            r2 = mod.get_tool_registry()
            assert r1 is not r2  # should be a copy
        finally:
            self._teardown_mock_registry(mod)

    def test_get_tool_function_found(self):
        mod = self._setup_mock_registry()
        try:
            fn = mod.get_tool_function("printer_status")
            assert callable(fn)
            assert fn() == {"status": "ok"}
        finally:
            self._teardown_mock_registry(mod)

    def test_get_tool_function_not_found(self):
        mod = self._setup_mock_registry()
        try:
            with pytest.raises(KeyError, match="Unknown tool"):
                mod.get_tool_function("nonexistent_tool")
        finally:
            self._teardown_mock_registry(mod)

    def test_get_all_tool_schemas_essential_filters(self):
        mod = self._setup_mock_registry()
        try:
            # Only printer_status is in TIER_ESSENTIAL, printer_files also is.
            schemas = mod.get_all_tool_schemas(tier="essential")
            names = [s["function"]["name"] for s in schemas]
            # Both printer_status and printer_files are in TIER_ESSENTIAL
            assert "printer_status" in names
            assert "printer_files" in names
        finally:
            self._teardown_mock_registry(mod)

    def test_get_all_tool_schemas_full_returns_all_cached(self):
        mod = self._setup_mock_registry()
        try:
            schemas = mod.get_all_tool_schemas(tier="full")
            # With our mock cache of 2 tools, only those in TIER_FULL are returned
            assert isinstance(schemas, list)
        finally:
            self._teardown_mock_registry(mod)

    def test_get_all_tool_schemas_bad_tier_raises(self):
        mod = self._setup_mock_registry()
        try:
            with pytest.raises(KeyError, match="Unknown tier"):
                mod.get_all_tool_schemas(tier="ultra")
        finally:
            self._teardown_mock_registry(mod)

    def test_mcp_tool_to_openai_schema_cached(self):
        mod = self._setup_mock_registry()
        try:
            def printer_status():
                pass

            schema = mod.mcp_tool_to_openai_schema(printer_status)
            assert schema["function"]["name"] == "printer_status"
        finally:
            self._teardown_mock_registry(mod)

    def test_mcp_tool_to_openai_schema_uncached_fallback(self):
        mod = self._setup_mock_registry()
        try:
            def custom_tool(x: int) -> str:
                """My custom tool."""
                pass

            schema = mod.mcp_tool_to_openai_schema(custom_tool)
            assert schema["function"]["name"] == "custom_tool"
            assert schema["function"]["description"] == "My custom tool."
        finally:
            self._teardown_mock_registry(mod)
