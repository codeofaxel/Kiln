"""A slice aimed at a printer nobody registered is refused by name.

2026-11-03 lap: ``slice_model(printer_name="no-such-printer")`` sliced with
the slicer's generic profile and said nothing (the name fell through the
model resolver's ``except`` and the slice went on), while every control door
answers ``PRINTER_NOT_FOUND``.  A typo at the slice door made a file for the
wrong machine with no word said.  The suite missed it because every slice
test either names no printer or registers the one it names.

Each door is called through the registered tool on a fake MCP, as an agent
calls it.  Fails at public 730b38e5; passes with ``_refuse_unknown_printer``
in front of the four doors.
"""
from __future__ import annotations

from typing import Any

import pytest


def _registered(plugin_cls: type) -> dict[str, Any]:
    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    plugin_cls().register(_FakeMcp())
    return tools


def _doors() -> dict[str, Any]:
    from kiln.plugins.estimate_tools import _EstimateToolsPlugin
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    doors = _registered(_SlicerToolsPlugin)
    doors.update(_registered(_EstimateToolsPlugin))
    return doors


DOORS = ("slice_model", "reslice_with_overrides", "slice_and_print", "slice_and_estimate")


@pytest.mark.parametrize("door", DOORS)
def test_a_name_nobody_registered_is_refused_before_the_slicer_runs(door, tmp_path, monkeypatch):
    import kiln.server as srv

    def _never_slices(*_a, **_k):  # pragma: no cover - the assertion is that this is never reached
        raise AssertionError("the slicer ran for a printer Kiln has no record of")

    monkeypatch.setattr("kiln.slicer.slice_file", _never_slices, raising=False)
    missing = tmp_path / "part.stl"
    result = _doors()[door](input_path=str(missing), printer_name="no-such-printer")
    assert result["success"] is False, result
    assert result["error"]["code"] == "PRINTER_NOT_FOUND", result
    assert "no-such-printer" in result["error"]["message"]
    assert srv._unknown_printer_error("no-such-printer", "x")["error"]["code"] == "PRINTER_NOT_FOUND"


@pytest.mark.parametrize("door", DOORS)
def test_an_unaimed_call_is_not_refused_by_the_printer_gate(door, tmp_path):
    """The control: with no printer named, the door goes on to judge the file
    itself (here: a file that does not exist), never ``PRINTER_NOT_FOUND``."""
    result = _doors()[door](input_path=str(tmp_path / "part.stl"))
    assert result["success"] is False
    assert result["error"]["code"] != "PRINTER_NOT_FOUND", result
