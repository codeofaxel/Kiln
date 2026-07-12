"""set_fan — set part-cooling / auxiliary / chamber fan speed on Bambu.

Covers the adapter (the exact ``M106 P<n> S<0-255>`` MQTT payload) and the MCP
tool (success + guard paths), without touching a live printer.
"""

from __future__ import annotations

import pytest

from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import PrinterError


def _bare_adapter():
    """A BambuAdapter with __init__ bypassed and publishing captured.

    The adapter's __init__ opens MQTT; we only exercise the pure command
    builder, so construct via __new__ and stub the two primitives it uses.
    """
    a = BambuAdapter.__new__(BambuAdapter)
    published: list[dict] = []
    a._publish_command = published.append  # type: ignore[attr-defined]
    a._next_seq = lambda: "42"  # type: ignore[attr-defined]
    return a, published


def test_part_fan_publishes_exact_payload():
    a, published = _bare_adapter()
    assert a.set_fan("part", 100) is True
    assert published == [
        {"print": {"sequence_id": "42", "command": "gcode_line", "param": "M106 P1 S255"}}
    ]


def test_aux_fan_maps_to_p2():
    a, published = _bare_adapter()
    a.set_fan("aux", 100)
    assert published[0]["print"]["param"] == "M106 P2 S255"


def test_chamber_fan_maps_to_p3():
    a, published = _bare_adapter()
    a.set_fan("chamber", 100)
    assert published[0]["print"]["param"] == "M106 P3 S255"


def test_percent_scales_to_0_255():
    a, published = _bare_adapter()
    a.set_fan("part", 0)
    a.set_fan("part", 20)
    assert published[0]["print"]["param"] == "M106 P1 S0"
    assert published[1]["print"]["param"] == "M106 P1 S51"


def test_node_aliases_accepted():
    a, published = _bare_adapter()
    a.set_fan("part_cooling", 100)
    a.set_fan("cooling", 100)
    a.set_fan("auxiliary", 100)
    assert [p["print"]["param"] for p in published] == [
        "M106 P1 S255",
        "M106 P1 S255",
        "M106 P2 S255",
    ]


def test_node_is_case_insensitive():
    a, published = _bare_adapter()
    a.set_fan("Chamber", 100)
    assert published[0]["print"]["param"] == "M106 P3 S255"


def test_unknown_node_raises():
    a, _ = _bare_adapter()
    with pytest.raises(PrinterError):
        a.set_fan("nozzle", 100)


def test_percent_out_of_range_raises():
    a, _ = _bare_adapter()
    with pytest.raises(PrinterError):
        a.set_fan("part", 101)
    with pytest.raises(PrinterError):
        a.set_fan("part", -1)


# --- the MCP tool -----------------------------------------------------------


def _call_tool(monkeypatch, adapter, node, percent):
    from kiln import server

    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    return server.set_fan(node, percent)


def test_tool_success(monkeypatch):
    calls: list[tuple[str, int]] = []

    class _Stub:
        def set_fan(self, node, percent):
            calls.append((node, percent))
            return True

    out = _call_tool(monkeypatch, _Stub(), "aux", 60)
    assert out["success"] is True
    assert out["node"] == "aux"
    assert out["percent"] == 60
    assert calls == [("aux", 60)]


def test_tool_unsupported_printer(monkeypatch):
    class _NoFan:
        pass  # e.g. an OctoPrint/Moonraker adapter

    out = _call_tool(monkeypatch, _NoFan(), "part", 100)
    assert out.get("success") is not True
    assert out["error"]["code"] == "UNSUPPORTED"


def test_tool_bad_input_is_errored(monkeypatch):
    class _Stub:
        def set_fan(self, node, percent):
            raise PrinterError("set_fan: percent must be 0-100, got 500.")

    out = _call_tool(monkeypatch, _Stub(), "part", 500)
    assert out.get("success") is not True
    assert "error" in out
