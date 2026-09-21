"""A ``Tool`` object's schema is read through ``mcp_compat`` on either SDK.

SDK 2 renamed the field ``input_schema`` and kept ``inputSchema`` only as a
serialisation alias, which plain attribute access does not see.  The
``tools/list`` mutator that strips ``default`` from every published schema
read ``tool.inputSchema``, found nothing on SDK 2, and quietly published
every default — the one thing it exists to prevent.  Each test here runs a
tool of the OTHER major's shape too, so the check does not depend on which
SDK the machine happens to have.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiln.mcp_compat import set_tool_input_schema, tool_input_schema

_SCHEMA = {
    "type": "object",
    "properties": {"printer_name": {"type": "string", "default": None, "title": "Printer Name"}},
}


def _sdk1_tool():
    return SimpleNamespace(name="t", description="", inputSchema=dict(_SCHEMA))


def _sdk2_tool():
    return SimpleNamespace(name="t", description="", input_schema=dict(_SCHEMA))


class TestTheAccessor:
    @pytest.mark.parametrize("tool", [_sdk1_tool(), _sdk2_tool()], ids=["sdk1", "sdk2"])
    def test_reads_whichever_attribute_the_sdk_uses(self, tool):
        assert tool_input_schema(tool) == _SCHEMA

    @pytest.mark.parametrize("make", [_sdk1_tool, _sdk2_tool], ids=["sdk1", "sdk2"])
    def test_writes_back_under_the_same_attribute(self, make):
        tool = make()
        set_tool_input_schema(tool, {"type": "object"})
        assert tool_input_schema(tool) == {"type": "object"}
        assert {k for k in vars(tool) if "chema" in k} == {k for k in vars(make()) if "chema" in k}

    def test_the_installed_sdk_builds_a_tool_the_accessor_can_read(self):
        from mcp.types import Tool

        tool = Tool(name="t", inputSchema=dict(_SCHEMA))
        assert tool_input_schema(tool) == _SCHEMA
        set_tool_input_schema(tool, {"type": "object"})
        assert tool.model_dump(by_alias=True)["inputSchema"] == {"type": "object"}

    def test_an_object_with_no_schema_is_none_and_refuses_a_write(self):
        assert tool_input_schema(SimpleNamespace(name="t")) is None
        with pytest.raises(AttributeError):
            set_tool_input_schema(SimpleNamespace(name="t"), {})


class TestThePublishedSchemaMutatorStripsDefaultsOnBothShapes:
    @pytest.mark.parametrize("make", [_sdk1_tool, _sdk2_tool], ids=["sdk1", "sdk2"])
    def test_default_is_gone_from_the_published_schema(self, make):
        from kiln import server

        tool = make()
        server._publish_schemas_without_defaults([tool])
        published = tool_input_schema(tool)
        assert "default" not in published["properties"]["printer_name"], published
        assert published["properties"]["printer_name"]["title"] == "Printer Name"


class TestTheAgentLoopReadsTheSameDoor:
    @pytest.mark.parametrize("make", [_sdk1_tool, _sdk2_tool], ids=["sdk1", "sdk2"])
    def test_the_openai_export_carries_the_schema_on_either_shape(self, make, monkeypatch):
        from kiln import agent_loop

        class _Server:
            async def list_tools(self):
                return [make()]

        monkeypatch.setattr(agent_loop, "_tool_cache", None)
        monkeypatch.setattr(agent_loop, "_get_mcp_server", lambda: _Server())
        cache = agent_loop._ensure_tool_cache()
        monkeypatch.setattr(agent_loop, "_tool_cache", None)
        assert cache["t"]["schema"]["function"]["parameters"] == _SCHEMA
