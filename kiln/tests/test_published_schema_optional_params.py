"""An omitted optional argument is accepted at the wire, and the schema
Kiln publishes never invites a host to refuse one.

Measured 2026-09-15 against the running server: ``printer_status(detail=
"full")`` was refused for the omitted ``printer_name`` with ``expected
nonoptional, received undefined``, and ``load_filament`` for every
``Optional`` it was not handed — mid-load, on a real printer.  The refusal
was the host's own validator, built from the schema Kiln published; the
same call with the argument given as an explicit ``null`` went through, and
a property carrying no ``default`` keyword went through omitted.  So the
one thing under Kiln's control is the ``default`` keyword on the wire: the
server applies the default whether or not the schema advertises it.

These tests drive the real lowlevel ``tools/list`` and ``tools/call``
handlers — the objects every transport hands a client — not the Python
functions behind them.
"""

from __future__ import annotations

import asyncio

import pytest

from kiln import server
from kiln.mcp_compat import MCP_SDK_MAJOR, lowlevel_server


@pytest.fixture(autouse=True)
def _terms_accepted(monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)


def _tools_on_the_wire() -> dict:
    """``{name: Tool}`` exactly as a client receives it from ``tools/list``."""
    srv = lowlevel_server(server.mcp)
    if MCP_SDK_MAJOR >= 2:
        handler = srv.get_request_handler("tools/list").handler
        resp = asyncio.run(handler(None, None))
        return {t.name: t for t in resp.tools}
    from mcp.types import ListToolsRequest

    req = ListToolsRequest(method="tools/list")
    resp = asyncio.run(srv.request_handlers[ListToolsRequest](req))
    return {t.name: t for t in resp.root.tools}


def _call_on_the_wire(name: str, arguments: dict):
    """The ``CallToolResult`` a client receives from ``tools/call``."""
    from mcp.types import CallToolRequestParams

    srv = lowlevel_server(server.mcp)
    params = CallToolRequestParams(name=name, arguments=arguments)
    if MCP_SDK_MAJOR >= 2:
        handler = srv.get_request_handler("tools/call").handler
        return asyncio.run(handler(None, params))
    from mcp.types import CallToolRequest

    req = CallToolRequest(method="tools/call", params=params)
    return asyncio.run(srv.request_handlers[CallToolRequest](req)).root


def _registry_properties(name: str) -> dict:
    return server.mcp._tool_manager._tools[name].parameters["properties"]


def test_the_symptom_tools_publish_their_optionals_without_a_default():
    wire = _tools_on_the_wire()

    status = wire["printer_status"].inputSchema
    assert "printer_name" not in (status.get("required") or [])
    assert "default" not in status["properties"]["printer_name"]
    assert "default" not in status["properties"]["detail"]
    # A non-null default is still told to the agent, in the one field the
    # measured host preserves.
    assert 'Default: "full".' in status["properties"]["detail"]["description"]

    load = wire["load_filament"].inputSchema
    for optional in ("material", "temperature", "length_mm", "printer_name"):
        assert optional not in (load.get("required") or [])
        assert "default" not in load["properties"][optional], optional


def test_the_registry_schema_keeps_its_defaults():
    """The wire copy is a new dict; validation and the OpenAI export still
    read the full schema from the registry."""
    _tools_on_the_wire()
    assert _registry_properties("printer_status")["detail"]["default"] == "full"
    assert _registry_properties("printer_status")["printer_name"]["default"] is None


def test_no_published_parameter_carries_the_default_keyword():
    wire = _tools_on_the_wire()
    registry_defaults = 0
    offenders: list[str] = []
    for name, tool in wire.items():
        for pname, prop in (tool.inputSchema.get("properties") or {}).items():
            if "default" in _registry_properties(name).get(pname, {}):
                registry_defaults += 1
            if "default" in prop:
                offenders.append(f"{name}.{pname}")
            if pname in (tool.inputSchema.get("required") or []) and (
                "default" in _registry_properties(name).get(pname, {})
            ):
                offenders.append(f"{name}.{pname} (required despite a default)")
    assert len(wire) > 100, "the whole registry must be on the wire"
    assert registry_defaults > 0, "vacuous: no defaulted parameter registered"
    assert offenders == [], offenders[:20]


def test_an_omitted_optional_reaches_the_tool_at_the_wire():
    """Passes before and after the schema change — it pins that Kiln's own
    validation was never the refuser, so a future regression on this side
    is caught at the same door."""
    result = _call_on_the_wire("get_material", {})
    assert not result.isError, result.content
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "INVALID_ARGS" not in text
    assert "invalid arguments" not in text
