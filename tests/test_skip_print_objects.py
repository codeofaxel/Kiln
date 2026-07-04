"""skip_print_objects — abandon failed objects on a multi-object plate mid-print.

Covers each adapter's skip command (Bambu MQTT, Klipper EXCLUDE_OBJECT,
OctoPrint M486) and the Pro-gated MCP tool (success, guards, honest
unsupported, and the tier gate), without touching a live printer.
"""

from __future__ import annotations

import pytest

from kiln.printers.base import PrinterError


# --- Bambu adapter (MQTT skip_objects) --------------------------------------


def _bambu():
    from kiln.printers.bambu import BambuAdapter

    a = BambuAdapter.__new__(BambuAdapter)
    published: list[dict] = []
    a._publish_command = published.append  # type: ignore[attr-defined]
    a._next_seq = lambda: "42"  # type: ignore[attr-defined]
    return a, published


def test_bambu_publishes_exact_payload():
    a, published = _bambu()
    assert a.skip_objects([724, 757]) is True
    assert published == [
        {"print": {"sequence_id": "42", "command": "skip_objects", "obj_list": [724, 757]}}
    ]


def test_bambu_coerces_ids_to_int():
    a, published = _bambu()
    a.skip_objects(["724", 757])  # strings from a JSON client
    assert published[0]["print"]["obj_list"] == [724, 757]


def test_bambu_empty_raises():
    a, _ = _bambu()
    with pytest.raises(PrinterError):
        a.skip_objects([])


# --- Moonraker / Klipper adapter (EXCLUDE_OBJECT) ---------------------------


def test_moonraker_sends_exclude_object():
    from kiln.printers.moonraker import MoonrakerAdapter

    a = MoonrakerAdapter.__new__(MoonrakerAdapter)
    sent: list[list[str]] = []
    a.send_gcode = lambda cmds: (sent.append(cmds) or True)  # type: ignore[attr-defined]
    assert a.skip_objects(["Part1", "Part2"]) is True
    assert sent == [["EXCLUDE_OBJECT NAME=Part1", "EXCLUDE_OBJECT NAME=Part2"]]


def test_moonraker_empty_raises():
    from kiln.printers.moonraker import MoonrakerAdapter

    a = MoonrakerAdapter.__new__(MoonrakerAdapter)
    a.send_gcode = lambda cmds: True  # type: ignore[attr-defined]
    with pytest.raises(PrinterError):
        a.skip_objects(["  ", ""])  # all-blank names


# --- OctoPrint adapter (M486) ----------------------------------------------


def test_octoprint_sends_m486():
    from kiln.printers.octoprint import OctoPrintAdapter

    a = OctoPrintAdapter.__new__(OctoPrintAdapter)
    posts: list[tuple] = []
    a._post = lambda path, json=None: posts.append((path, json))  # type: ignore[attr-defined]
    assert a.skip_objects([0, 2]) is True
    assert posts == [("/api/printer/command", {"commands": ["M486 P0", "M486 P2"]})]


def test_octoprint_non_integer_raises():
    from kiln.printers.octoprint import OctoPrintAdapter

    a = OctoPrintAdapter.__new__(OctoPrintAdapter)
    a._post = lambda *a, **k: None  # type: ignore[attr-defined]
    with pytest.raises(PrinterError):
        a.skip_objects(["not-an-index"])


# --- Serial / USB Marlin adapter (M486) -------------------------------------


def test_serial_sends_m486():
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    a = SerialPrinterAdapter.__new__(SerialPrinterAdapter)
    sent: list[list[str]] = []
    a.send_gcode = lambda cmds: (sent.append(cmds) or True)  # type: ignore[attr-defined]
    assert a.skip_objects([0, 2]) is True
    assert sent == [["M486 P0", "M486 P2"]]


def test_serial_empty_raises():
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    a = SerialPrinterAdapter.__new__(SerialPrinterAdapter)
    a.send_gcode = lambda cmds: True  # type: ignore[attr-defined]
    with pytest.raises(PrinterError):
        a.skip_objects([])


# --- the Pro-gated MCP tool -------------------------------------------------


def _allow_pro(monkeypatch):
    # The @requires_tier(PRO) gate calls check_tier() — force allow.
    monkeypatch.setattr("kiln_pro.enterprise.licensing.check_tier", lambda t: (True, ""))


def _call_tool(monkeypatch, adapter, ids, allow=True, **kw):
    from kiln import server

    if allow:
        _allow_pro(monkeypatch)
    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    return server.skip_print_objects(ids, **kw)


def test_tool_success_passes_ids_through_untouched(monkeypatch):
    calls: list[list] = []

    class _Stub:
        def skip_objects(self, object_ids):
            calls.append(object_ids)
            return True

    # Klipper-style string names must survive un-coerced.
    out = _call_tool(monkeypatch, _Stub(), ["Part1", "757"])
    assert out["success"] is True
    assert out["skipped_objects"] == ["Part1", "757"]
    assert calls == [["Part1", "757"]]


def test_tool_pro_gate_blocks_free(monkeypatch):
    from kiln import server

    monkeypatch.setattr("kiln_pro.enterprise.licensing.check_tier", lambda t: (False, "nope"))

    class _Stub:
        def skip_objects(self, object_ids):  # pragma: no cover - must not run
            raise AssertionError("free tier reached the adapter")

    monkeypatch.setattr(server, "_get_adapter", lambda: _Stub())
    out = server.skip_print_objects(["757"])
    assert out.get("success") is not True
    assert out.get("code") == "TIER_REQUIRED"


def test_tool_empty_is_guarded(monkeypatch):
    class _Stub:
        def skip_objects(self, object_ids):  # pragma: no cover - must not run
            raise AssertionError("should not reach the adapter")

    out = _call_tool(monkeypatch, _Stub(), [])
    assert out["error"]["code"] == "NO_OBJECTS"


def test_tool_unsupported_printer(monkeypatch):
    class _NoSkip:  # e.g. Prusa Link or Elegoo
        pass

    out = _call_tool(monkeypatch, _NoSkip(), ["1"])
    assert out["error"]["code"] == "UNSUPPORTED"
