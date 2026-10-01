"""Every diagnosis door gathers its signals one way.

``diagnose_print_failure_live`` and ``retry_print_with_fix`` each built the
dict ``diagnose_from_signals`` reads, and both copies carried the same two
silent faults: the printer profile read as a dict (it is a ``PrinterIntel``,
so the read raised into a best-effort ``except``) and
``report.bridging.max_bridge_length`` (the field is ``max_bridge_length_mm``).
The first left every retry deaf to the printer's enclosure and failure
modes; the second meant no long bridge ever reached a verdict, at either
door.  Both doors now call ``kiln.printability.collect_failure_signals``.

Each test drives the registered TOOL against a paused printer holding its
temperatures, so the thermal check stays quiet and the signal under test is
what decides.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from kiln.printers.base import PrinterState, PrinterStatus

pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the fixtures


class _MCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *_args: Any, **_kwargs: Any):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _tool(plugin_module: str, plugin_class: str, name: str):
    import importlib

    mcp = _MCP()
    getattr(importlib.import_module(plugin_module), plugin_class)().register(mcp)
    return mcp.tools[name]


@pytest.fixture
def free_tier(monkeypatch):
    """No kiln-pro, submodules the conftest loaded included."""
    import kiln.printer_intelligence as pi

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


@pytest.fixture
def paused_printer(monkeypatch):
    import kiln.server as srv

    state = PrinterState(
        connected=True,
        state=PrinterStatus.PAUSED,
        tool_temp_actual=248.0,
        tool_temp_target=250.0,
        bed_temp_actual=100.0,
        bed_temp_target=100.0,
    )
    adapter = types.SimpleNamespace(get_state=lambda: state)
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: adapter)
    return adapter


def _bridge_stl(path: Path) -> str:
    """Two 10 x 10 x 20 mm pillars 40 mm apart, joined by a 2 mm deck."""
    from trimesh.creation import box

    body = None
    for x in (-25.0, 25.0):
        pillar = box(extents=(10.0, 10.0, 20.0))
        pillar.apply_translation((x, 0, 10.0))
        body = pillar if body is None else body.union(pillar)
    deck = box(extents=(60.0, 10.0, 2.0))
    deck.apply_translation((0, 0, 20.0 + 1.0 - 0.01))
    body = body.union(deck)
    body.export(str(path))
    return str(path)


def _block_stl(path: Path) -> str:
    from trimesh.creation import box

    block = box(extents=(30.0, 30.0, 10.0))
    block.apply_translation((0, 0, 5.0))
    block.export(str(path))
    return str(path)


def test_a_long_bridge_reaches_the_live_diagnosis(tmp_path, free_tier, paused_printer):
    from kiln.printability import analyze_printability

    model = _bridge_stl(tmp_path / "bridge.stl")
    assert analyze_printability(model).bridging.max_bridge_length_mm > 15  # the premise

    diagnose = _tool("kiln.plugins.printability_tools", "_PrintabilityToolsPlugin", "diagnose_print_failure_live")
    result = diagnose(model_path=model, material="pla")

    assert result["success"], result
    diagnosis = result["diagnosis"]
    assert diagnosis["signals"]["max_bridge_mm"] > 15
    assert diagnosis["failure_category"] == "geometry", diagnosis


def test_a_retry_hears_an_open_frame_printer(tmp_path, monkeypatch, free_tier, paused_printer):
    """ABS on an open-frame A1: the diagnosis is the material-environment
    mismatch, and its slicer fix reaches the retry's re-slice."""
    import kiln.slicer as slicer
    import kiln.slicer_profiles as profiles
    from kiln.printer_intelligence import get_printer_intel

    assert get_printer_intel("bambu_a1").has_enclosure is False  # the premise
    handed: dict[str, Any] = {}

    def capture_profile(_printer_id, overrides=None, **_kwargs):
        handed.update(overrides or {})
        return None

    def stop_slice(*_args, **_kwargs):
        raise slicer.SlicerError("stopped by the test after the overrides were handed over")

    monkeypatch.setattr(profiles, "resolve_slicer_profile", capture_profile)
    monkeypatch.setattr(profiles, "start_gcode_override_from_printer", lambda *_a, **_k: (None, "test"))
    monkeypatch.setattr(slicer, "slice_file", stop_slice)

    retry = _tool("kiln.plugins.smart_print_tools", "_SmartPrintToolsPlugin", "retry_print_with_fix")
    retry(
        model_path=_block_stl(tmp_path / "block.stl"),
        material="ABS",
        printer_id="bambu_a1",
        skip_validation=True,
    )

    assert handed.get("brim_width") == "8", handed
