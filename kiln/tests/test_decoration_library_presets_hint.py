"""list_decorations points at the OTHER saved-decoration store.

Kiln has two stores the product calls "decorations": this library
(name-keyed, adapts to material) and kiln-pro's decoration PRESETS
(id-keyed, versioned — what the web's /decorations pages show).  A user
who calls ``list_decorations`` and sees one lava texture, with their
saved logo preset nowhere and no pointer to it, concludes the cloud
library doesn't exist (2026-09-01 investigation).

The fix is a HINT, not a merge: when kiln-pro's preset store holds
entries, the listing names the count and the tool that lists them.  The
two stores stay separate on purpose — the library adapts, the preset
remembers — and without kiln-pro installed the listing is byte-for-byte
what it always was.
"""
from __future__ import annotations

import sys
import types

import pytest


def _list_tool():
    from kiln.plugins.decoration_library_tools import plugin

    class _MockMCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorator

    mcp = _MockMCP()
    plugin.register(mcp)
    return mcp.tools["list_decorations"]


_MOD = "kiln_pro.design_versions.decoration_presets"


def _stub_preset_store(monkeypatch, preset_count):
    """Install a fake kiln_pro preset-store module (or a raising one)."""
    mod = types.ModuleType(_MOD)

    class DecorationPresetStore:
        def list_presets(self, **kw):
            if isinstance(preset_count, Exception):
                raise preset_count
            return [object()] * preset_count

        def close(self):
            pass

    mod.DecorationPresetStore = DecorationPresetStore
    monkeypatch.setitem(sys.modules, _MOD, mod)


def test_no_kiln_pro_no_hint(monkeypatch):
    # sys.modules[name] = None makes `import name` raise ImportError —
    # the free-install world, where the listing must be unchanged.
    monkeypatch.setitem(sys.modules, _MOD, None)
    out = _list_tool()()
    assert out["success"] is True
    assert "presets_hint" not in out


def test_hint_names_count_and_tool(monkeypatch):
    _stub_preset_store(monkeypatch, 3)
    out = _list_tool()()
    assert out["success"] is True
    assert "3" in out["presets_hint"]
    assert "list_decoration_presets" in out["presets_hint"]


def test_empty_preset_store_no_hint(monkeypatch):
    _stub_preset_store(monkeypatch, 0)
    out = _list_tool()()
    assert "presets_hint" not in out


def test_broken_preset_store_never_breaks_the_listing(monkeypatch):
    _stub_preset_store(monkeypatch, RuntimeError("db locked"))
    out = _list_tool()()
    assert out["success"] is True
    assert "presets_hint" not in out
