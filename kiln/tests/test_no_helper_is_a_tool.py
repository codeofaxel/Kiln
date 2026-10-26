"""A private helper is never a tool, and a helper placed above a tool does not
take its decorator.

A helper written between ``@mcp.tool()`` and the function it decorated took
the decorator: the registry gained ``_photo_relief_auto_depth`` and lost
``decorate_surface``, so every agent lost the tool that presses a photo or a
logo into a part, and nothing else failed.
"""

from __future__ import annotations

from kiln.server import mcp


def _registered() -> set[str]:
    return set(mcp._tool_manager._tools)


def test_no_registered_tool_is_a_private_helper():
    assert sorted(n for n in _registered() if n.startswith("_")) == []


def test_the_surface_decoration_tool_is_registered():
    assert "decorate_surface" in _registered()
