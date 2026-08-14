"""Every tool the connect preamble teaches is findable mid-session.

Kiln teaches agents its capabilities through three hand-written surfaces:

1. the connect-time instructions (``kiln.server._build_instructions``) —
   read once by the client, then effectively lost to a long session;
2. ``get_started()`` — callable any time;
3. ``get_skill_manifest()`` — callable any time.

They are three copies of the same knowledge, each free to drift.  The
measured failure: ``restart_server`` was taught ONLY in the preamble, so a
mid-session agent could not rediscover it — you cannot ToolSearch a name
you do not know exists — and users were told to quit and reopen their
client app while the tool built for exactly that sat unused.

The invariant here is mechanical so there is no second hand-list to rot:
whatever the preamble names as a tool, the callable surfaces must also
name.  Adding a tool to the preamble without adding it to a callable
surface turns this red.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

# Tool-shaped names in backticks, e.g. `restart_server()` or
# `visualize_model(file_path)`.  Bare CLI examples (`kiln slice ...`)
# deliberately do not match: they are not MCP tool names.
_TOOL_IN_BACKTICKS = re.compile(r"`([a-z_][a-z0-9_]*)\(")


def _preamble_tool_names() -> set[str]:
    from kiln.server import _build_instructions

    return set(_TOOL_IN_BACKTICKS.findall(_build_instructions()))


def _callable_surfaces_text() -> str:
    from kiln.plugins.utility_tools import _UtilityToolsPlugin
    from kiln.skill_manifest import SkillManifest

    tools: dict = {}

    class MockMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    _UtilityToolsPlugin().register(MockMCP())
    with patch("kiln.version_check.check_for_update", return_value=None):
        started = tools["get_started"]()
    return json.dumps(started) + json.dumps(SkillManifest().to_dict())


def test_every_preamble_tool_is_discoverable_mid_session():
    names = _preamble_tool_names()
    # The preamble must actually name tools, or this test is vacuous and
    # the extraction regex has drifted from the preamble's house style.
    assert len(names) >= 5, (
        f"only {sorted(names)} extracted from the preamble — if its "
        "formatting changed, update _TOOL_IN_BACKTICKS to match"
    )

    surfaces = _callable_surfaces_text()
    lost = sorted(n for n in names if n not in surfaces)
    assert not lost, (
        "taught at connect but unfindable afterwards: "
        f"{lost}. A tool named in _build_instructions must also appear in "
        "get_started() or the skill manifest — the preamble is read once "
        "and lost, and an agent cannot search for a name it never saw."
    )


@pytest.mark.parametrize("tool", ["restart_server", "trim_serve_processes"])
def test_the_session_maintenance_tools_are_in_both_callable_surfaces(tool):
    """The named regression, pinned tighter than the general invariant.

    These are the tools that keep the session itself healthy.  They are
    useful precisely mid-session — after a code change, after noticing
    leftover processes — which is when the preamble is long gone, so
    'in at least one surface' is not enough for them: both callable
    surfaces carry them.
    """
    from kiln.plugins.utility_tools import _UtilityToolsPlugin
    from kiln.skill_manifest import SkillManifest

    tools: dict = {}

    class MockMCP:
        def tool(self):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    _UtilityToolsPlugin().register(MockMCP())
    with patch("kiln.version_check.check_for_update", return_value=None):
        started = tools["get_started"]()

    assert tool in json.dumps(started), f"{tool} missing from get_started()"
    assert tool in json.dumps(SkillManifest().to_dict()), (
        f"{tool} missing from the skill manifest"
    )
