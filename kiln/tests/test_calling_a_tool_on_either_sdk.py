"""A registered tool is called the same way on both SDK majors.

The tool manager's ``call_tool`` grew a REQUIRED ``context`` parameter in SDK
2.  A test written against SDK 1 — where it defaults to ``None`` — passes on
the developer's machine and raises ``TypeError: call_tool() missing 1
required positional argument: 'context'`` in CI, which installs SDK 2.  That
is exactly what happened to the monitor's poll test (four Python versions red
on main, 2026-09-24), and it is the shape ``kiln.mcp_compat`` exists to stop:
one reader that knows both spellings, rather than every caller guessing.

These drive the helper against BOTH manager shapes, built here, so neither
needs the other SDK installed to be proven.
"""

from __future__ import annotations

import anyio
import pytest

from kiln.mcp_compat import call_registered_tool


class _ManagerSdk1:
    """SDK 1: ``context`` is optional."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def call_tool(self, name, arguments, context=None, convert_result=False):
        self.calls.append((name, arguments, context))
        return {"ok": name}


class _ManagerSdk2:
    """SDK 2: ``context`` is required, and still called ``context``."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def call_tool(self, name, arguments, context, convert_result=False):
        self.calls.append((name, arguments, context))
        return {"ok": name}


class _ManagerNoContext:
    """A third spelling: no ``context`` parameter at all."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": name}


class _Server:
    def __init__(self, manager):
        self._tool_manager = manager


@pytest.mark.parametrize("manager_cls", [_ManagerSdk1, _ManagerSdk2, _ManagerNoContext])
def test_a_tool_is_called_on_every_manager_shape(manager_cls):
    manager = manager_cls()
    result = anyio.run(call_registered_tool, _Server(manager), "kiln_monitor_snapshot", {"include_capabilities": True})
    assert result == {"ok": "kiln_monitor_snapshot"}
    assert manager.calls[0][0] == "kiln_monitor_snapshot"
    assert manager.calls[0][1] == {"include_capabilities": True}


def test_the_sdk_2_shape_is_the_one_a_plain_call_breaks_on():
    """The regression itself: two positional arguments raise on SDK 2's
    manager and are fine on SDK 1's.  The helper is what closes that gap."""
    sdk2 = _ManagerSdk2()
    with pytest.raises(TypeError, match="context"):
        anyio.run(sdk2.call_tool, "kiln_monitor_snapshot", {})
    anyio.run(_ManagerSdk1().call_tool, "kiln_monitor_snapshot", {})


def test_no_arguments_is_an_empty_mapping_not_none():
    manager = _ManagerSdk2()
    anyio.run(call_registered_tool, _Server(manager), "kiln_monitor_snapshot")
    assert manager.calls[0][1] == {}


def test_a_manager_may_be_passed_in_place_of_the_server():
    manager = _ManagerSdk2()
    anyio.run(call_registered_tool, manager, "kiln_monitor_snapshot", {})
    assert manager.calls[0][0] == "kiln_monitor_snapshot"
