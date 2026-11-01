"""Every tool Kiln registers describes every one of its inputs.

A model reads a tool's input schema to decide what to pass.  A parameter
with no ``description`` there is a name and a type and nothing else, so the
model guesses (a printer's name or its id, millimetres or metres), and a
directory grading the server marks the tool incomplete.

The descriptions live in the docstrings, in an ``Args:`` block or
``:param x:`` lines, and ``kiln.tool_inputs`` writes them into the schema as
each tool registers.  So a tool fails here for one reason only: its docstring
does not describe that parameter.  The fix is the docstring, never a
sentence invented here.

The served tools an install without kiln-pro lists as stubs take their
descriptions from the manifest kiln-pro generates, so a plain install says
the same sentence about each input as Kiln's servers do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kiln.server as server
from kiln import tool_inputs


def _registered_tools() -> dict[str, Any]:
    server._ensure_internal_tool_plugins_registered()
    return dict(server.mcp._tool_manager._tools)


def test_every_registered_tool_describes_every_input() -> None:
    """Public Kiln's own tools (kiln-pro's own test judges kiln-pro's)."""
    undescribed = []
    for name, tool in sorted(_registered_tools().items()):
        module = getattr(tool.fn, "__module__", "") or ""
        if module.startswith("kiln_pro"):
            continue
        missing = tool_inputs.undescribed(tool)
        if missing:
            undescribed.append(f"{name} ({module}): {', '.join(missing)}")
    assert undescribed == [], (
        f"{len(undescribed)} tools leave an input undescribed. Describe each one in the "
        "tool's docstring (an Args: block or a :param line):\n  " + "\n  ".join(undescribed)
    )


class _Catcher:
    """Takes what the stub registrar hands a server, through a real registry."""

    def __init__(self) -> None:
        from mcp.server.mcpserver import MCPServer

        self.server = MCPServer("stub-catcher")
        tool_inputs.install(self.server)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        return self.server.tool(*args, **kwargs)

    @property
    def tools(self) -> dict[str, Any]:
        return dict(self.server._tool_manager._tools)


def test_every_served_stub_describes_every_input_as_the_manifest_does() -> None:
    """A plain install's stub says, for each input, the manifest's sentence."""
    manifest = json.loads(
        (Path(server.__file__).parent / "pro_tool_manifest.json").read_text(encoding="utf-8")
    )
    catcher = _Catcher()
    server._register_pro_tool_stubs(catcher)
    served = {
        entry["name"]: entry
        for entry in manifest["tools"]
        if entry.get("served") is not False and not entry["name"].startswith("_")
    }
    assert served, "the manifest serves no tools; the check would prove nothing"
    wrong = []
    for name, entry in sorted(served.items()):
        stub = catcher.tools.get(name)
        if stub is None:
            wrong.append(f"{name}: no stub registered")
            continue
        stated = {
            pname: str(prop.get("description") or "")
            for pname, prop in (entry.get("parameters") or {}).get("properties", {}).items()
        }
        for pname, prop in stub.parameters.get("properties", {}).items():
            said = str(prop.get("description") or "")
            if not said.strip():
                wrong.append(f"{name}.{pname}: undescribed on the stub")
            elif pname == "run_actions" and pname not in stated:
                continue  # the stub's own switch: described by the registrar, absent from the manifest
            elif said != stated.get(pname):
                wrong.append(f"{name}.{pname}: the stub's description differs from the manifest's")
    assert wrong == [], "\n  ".join(["served stubs whose inputs are not described as the manifest describes them:", *wrong])


def test_the_parser_reads_both_docstring_styles() -> None:
    google = """Do a thing.

    Args:
        printer_name: Target printer.  Omit for the default
            printer.
        detail (str): ``"full"`` or ``"lite"``.
        empty:

    Returns:
        A dict.
    """
    assert tool_inputs.parse(google) == {
        "printer_name": "Target printer.  Omit for the default printer.",
        "detail": '``"full"`` or ``"lite"``.',
    }
    rest = """Do a thing.

    :param design_id: The design, by id or
        by name.
    :param int depth: How deep.
    :returns: A dict.
    """
    assert tool_inputs.parse(rest) == {
        "design_id": "The design, by id or by name.",
        "depth": "How deep.",
    }
    assert tool_inputs.parse(None) == {}


def test_a_described_input_reaches_the_schema_and_an_undescribed_one_is_named() -> None:
    from mcp.server.mcpserver import MCPServer

    m = MCPServer("t")
    assert tool_inputs.install(m) is True
    assert tool_inputs.install(m) is True, "installing twice is harmless"

    @m.tool()
    def half(said: str, unsaid: int = 0) -> dict:
        """One input described, one not.

        Args:
            said: The described one.
        """
        return {}

    tool = m._tool_manager._tools["half"]
    assert tool.parameters["properties"]["said"]["description"] == "The described one."
    assert tool_inputs.undescribed(tool) == ["unsaid"]
