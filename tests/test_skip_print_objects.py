"""skip_print_objects — abandon failed objects on a multi-object plate mid-print.

Covers the adapter (the exact Bambu ``skip_objects`` MQTT payload) and the MCP
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


def test_skip_objects_publishes_exact_payload():
    a, published = _bare_adapter()
    assert a.skip_objects([724, 757]) is True
    assert published == [
        {"print": {"sequence_id": "42", "command": "skip_objects", "obj_list": [724, 757]}}
    ]


def test_skip_objects_coerces_ids_to_int():
    a, published = _bare_adapter()
    a.skip_objects(["724", 757])  # strings from a JSON client
    assert published[0]["print"]["obj_list"] == [724, 757]
    assert all(isinstance(x, int) for x in published[0]["print"]["obj_list"])


def test_skip_objects_empty_raises():
    a, _ = _bare_adapter()
    with pytest.raises(PrinterError):
        a.skip_objects([])


def test_skip_objects_non_integer_raises():
    a, _ = _bare_adapter()
    with pytest.raises(PrinterError):
        a.skip_objects(["not-a-number"])


# --- the MCP tool -----------------------------------------------------------


def _call_tool(monkeypatch, adapter, ids, **kw):
    from kiln import server

    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    return server.skip_print_objects(ids, **kw)


def test_tool_success(monkeypatch):
    calls: list[list[int]] = []

    class _Stub:
        def skip_objects(self, object_ids):
            calls.append(object_ids)
            return True

    out = _call_tool(monkeypatch, _Stub(), [724, 757])
    assert out["success"] is True
    assert out["skipped_object_label_ids"] == [724, 757]
    assert calls == [[724, 757]]


def test_tool_empty_is_guarded(monkeypatch):
    class _Stub:
        def skip_objects(self, object_ids):  # pragma: no cover - must not run
            raise AssertionError("should not reach the adapter")

    out = _call_tool(monkeypatch, _Stub(), [])
    assert out.get("success") is not True
    assert out["error"]["code"] == "NO_OBJECTS"


def test_tool_unsupported_printer(monkeypatch):
    class _NoSkip:
        pass  # e.g. an OctoPrint/Moonraker adapter

    out = _call_tool(monkeypatch, _NoSkip(), [1])
    assert out.get("success") is not True
    assert out["error"]["code"] == "UNSUPPORTED"
