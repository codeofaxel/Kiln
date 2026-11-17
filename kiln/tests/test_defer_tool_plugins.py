"""KILN_DEFER_TOOL_PLUGINS=1 moves the tool plugins from import to first use.

``import kiln.server`` loads every tool plugin, so any process that imports
it sees the whole tool list.  A process that imports it to run one function
and serves no tools can set the switch before the import; the plugins then
load on the first call to ``_ensure_internal_tool_plugins_registered``.

Each case is a fresh interpreter, because the switch is read once, at import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

_CHILD = r"""
import functools, inspect, json, logging, sys
logging.disable(logging.CRITICAL)
sys.modules["kiln_pro"] = None   # a plain install: the paid package is absent
from kiln.mcp_compat import FastMCP
_init = FastMCP.__init__
@functools.wraps(_init)
def _patched(self, *args, **kwargs):
    keep = set(inspect.signature(_init).parameters)
    return _init(self, *args, **{k: v for k, v in kwargs.items() if k in keep})
FastMCP.__init__ = _patched

import kiln.server as server

def plugin_tools():
    return sum(
        1 for tool in server.mcp._tool_manager.list_tools()
        if (getattr(tool.fn, "__module__", "") or "").startswith("kiln.plugins")
    )

def all_tools():
    return len(server.mcp._tool_manager.list_tools())

before = (plugin_tools(), all_tools(), server._INTERNAL_TOOL_PLUGINS_REGISTERED)
server._ensure_internal_tool_plugins_registered()
after = (plugin_tools(), all_tools(), server._INTERNAL_TOOL_PLUGINS_REGISTERED)
print("RESULT " + json.dumps({"before": before, "after": after}))
"""


def _import_server(tmp_path: Path, switch: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "KILN_DEFER_TOOL_PLUGINS"}
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), *[p for p in sys.path if p]])
    env["HOME"] = str(tmp_path)
    if switch is not None:
        env["KILN_DEFER_TOOL_PLUGINS"] = switch
    done = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, text=True,
                          env=env, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT "):])


@pytest.fixture(scope="module")
def default_import(tmp_path_factory):
    return _import_server(tmp_path_factory.mktemp("default"), None)


@pytest.fixture(scope="module")
def deferred_import(tmp_path_factory):
    return _import_server(tmp_path_factory.mktemp("deferred"), "1")


def test_by_default_the_plugins_load_at_import(default_import):
    plugins, _total, flagged = default_import["before"]
    assert plugins > 0
    assert flagged is True
    assert default_import["after"] == default_import["before"]


def test_with_the_switch_no_plugin_loads_at_import(deferred_import):
    plugins, total, flagged = deferred_import["before"]
    assert plugins == 0
    assert flagged is False
    assert total > 0, "the tools the server module defines itself still register"


def test_with_the_switch_the_first_ask_loads_the_same_tools(default_import, deferred_import):
    assert deferred_import["after"] == default_import["after"]


def test_any_other_value_is_not_the_switch(tmp_path):
    for value in ("0", "", "true"):
        plugins, _total, flagged = _import_server(tmp_path, value)["before"]
        assert plugins > 0 and flagged is True, value
