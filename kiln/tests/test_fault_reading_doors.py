"""Every door that shows a Bambu fault reads it through one function.

Measured on a Bambu A1 (2026-09-16 night): the printer raised print_error
302022657 -- ``1200-8001``, "Cutting the filament failed. Please check to
see if the cutter is stuck." -- and ``printer_status`` answered "This is an
AMS lite filament load / unload fault, and Bambu publishes no page for it."
while the screen beside it had the message.  Kiln knew nothing about the
A1's filament cutter: the fact that the loud ram to the right end of the
rail IS the cut (the first move of every AMS load, switch and cancel) was
read as a crash three times that night and a print was killed over it.

The reading -- what the cutter does, why the blade misses, the reseat --
is know-how, and it lives in kiln-pro whoever it is free to.  Public Kiln
keeps the MECHANISM: one reader (``read_bambu_fault``) that asks kiln-pro
first through ``kiln._pro_fault_bridge`` and falls back to a family line
that says what kind of fault it is and where the reading is; and the door
wiring, so ``printer_status``, the filament-op results, the fault event and
``troubleshoot_printer`` all go through that reader and say the same thing.

Two conditions, both tested here because both are real installs:

* public only (the bridge is silent, which ``conftest`` makes the default):
  every door shows the family line and the pointer, and nothing in public
  Kiln carries a cause, a fix, or a forum URL for the three codes that
  moved;
* kiln-pro present (the bridge answers with a row): every door shows that
  row's cause, and the doors with room for a remedy show its fix.

The rows themselves are pinned on the kiln-pro side, against this public
checkout (``tests/test_cutter_fault_knowledge.py`` there).
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

import kiln._pro_fault_bridge as fault_bridge
from kiln.printers.bambu import (
    _BAMBU_PRINT_ERROR_FAULTS,
    BambuAdapter,
    BambuFaultReading,
    describe_bambu_filament_fault,
    read_bambu_fault,
)
from kiln.printers.base import PrinterState, PrinterStatus

#: The measured fault: the decimal the firmware publishes and the form the
#: printer's own screen renders it in.
CUTTER_FAULT_DECIMAL = 302022657
CUTTER_FAULT_RENDERED = "1200-8001"

#: What kiln-pro answers with, in the shape its catalog serves.  A stand-in,
#: not the real row: the real one is pinned in kiln-pro against this
#: checkout, and a public test must not depend on which kiln-pro is on the
#: path.  The words are chosen so a test can tell this row from the public
#: family line and from any other reading.
PRIVATE_ROW = {
    "title": "Cutting the filament failed",
    "cause": "The blade did not cross the filament path; after a teardown it is usually outside its slot.",
    "fix": "Power off cold, reseat the blade into the slot in the extruder body.",
    "severity": "warning",
    "namespace": "print_error",
}


@pytest.fixture
def bridge_answers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """kiln-pro present: the bridge returns PRIVATE_ROW for the cutter code."""
    asked: list[tuple[str, str]] = []

    def _decode(code: str, *, kind: str = "print_error") -> dict[str, Any] | None:
        asked.append((code, kind))
        digits = "".join(c for c in code.upper() if c in "0123456789ABCDEF")
        if kind == "print_error" and digits.startswith("12008001"):
            return dict(PRIVATE_ROW)
        return None

    monkeypatch.setattr(fault_bridge, "decode_fault", _decode)
    return asked


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BambuAdapter:
    """A Bambu adapter with a mocked-connected MQTT client."""
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(
        host="192.0.2.10", access_code="12345678", serial="TEST1234567890", timeout=2
    )
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    publish_result.rc = mqtt.MQTT_ERR_SUCCESS
    a._mqtt_client.publish.return_value = publish_result
    a._confirm_window_s = 0.0
    return a


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    """Feed *fields* in as a real ``push_status`` message."""
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _join_fault_notices(timeout: float = 5.0) -> None:
    import threading

    for t in threading.enumerate():
        if t.name == "kiln-fault-notice":
            t.join(timeout)


# ---------------------------------------------------------------------------
# 1. Public only: the family line, and nothing that moved
# ---------------------------------------------------------------------------


class TestPublicOnly:
    """The bridge is silent (conftest's default): public Kiln's own floor."""

    def test_the_cutter_code_gets_a_family_line_not_a_shrug(self) -> None:
        reading, url = describe_bambu_filament_fault(
            CUTTER_FAULT_RENDERED, kind="print_error"
        )

        # What kind of fault it is...
        assert "filament-cut fault" in reading
        # ...and where the reading is.
        assert "free with a Kiln sign-in" in reading
        # Not the generic family shrug this code used to get.
        assert "publishes no page" not in reading
        # A print_error never has a vendor page.
        assert url is None

    def test_printer_status_carries_the_family_line_beside_the_code(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=CUTTER_FAULT_DECIMAL)

        state = adapter.get_state()

        assert state.state is PrinterStatus.ERROR
        assert CUTTER_FAULT_RENDERED in state.fault_note
        assert "filament-cut fault" in state.fault_note
        # No fix to lead with, so the remedy is the clearing sentence alone.
        assert state.fault_remedy.startswith("Clear it on the printer's own screen")

    def test_the_cutter_line_carries_no_cause_fix_or_source(self) -> None:
        """The know-how is kiln-pro's; the line names the kind and points at it."""
        line = _BAMBU_PRINT_ERROR_FAULTS["12008001"].lower()

        assert "free with a kiln sign-in" in line
        for know_how in ("slot", "blade", "lever", "magnet", "hall", "http", "forum", "wiki"):
            assert know_how not in line, f"12008001 still carries {know_how!r}"

    def test_a_reading_with_no_bridge_is_public_and_carries_no_remedy(self) -> None:
        fault = read_bambu_fault(CUTTER_FAULT_RENDERED, kind="print_error")

        assert isinstance(fault, BambuFaultReading)
        assert fault.private is False
        assert fault.decoded is None
        assert fault.remedy is None

    def test_troubleshoot_printer_attaches_no_decode_without_kiln_pro(self) -> None:
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", hms_code=CUTTER_FAULT_RENDERED)

        assert out["hms_code"] == "1200_8001"
        assert out["hms_code_kind"] == "print_error"
        assert "hms_decoded" not in out
        assert "hms_wiki_url" not in out


# ---------------------------------------------------------------------------
# 2. kiln-pro present: every door shows the row, and the same row
# ---------------------------------------------------------------------------


class TestEveryDoorReadsTheSameRow:
    def test_the_reader_returns_the_row_as_reading_and_remedy(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        fault = read_bambu_fault(CUTTER_FAULT_RENDERED, kind="print_error")

        assert fault.private is True
        assert fault.reading == PRIVATE_ROW["cause"]
        assert fault.remedy == PRIVATE_ROW["fix"]
        assert fault.url is None
        assert fault.decoded == PRIVATE_ROW
        # Asked in the code's own namespace -- the same digits mean
        # something else in the other one.
        assert bridge_answers == [(CUTTER_FAULT_RENDERED, "print_error")]

    def test_printer_status_shows_the_cause_and_leads_the_remedy_with_the_fix(
        self, adapter: BambuAdapter, bridge_answers: list[tuple[str, str]]
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=CUTTER_FAULT_DECIMAL)

        state = adapter.get_state()

        assert CUTTER_FAULT_RENDERED in state.fault_note
        assert PRIVATE_ROW["cause"] in state.fault_note
        assert "filament-cut fault" not in state.fault_note
        # The fix first, then what clears the fault -- still its own field.
        assert state.fault_remedy.startswith(PRIVATE_ROW["fix"])
        assert "screen" in state.fault_remedy
        assert "clear_printer_error" in state.fault_remedy
        assert "clear_printer_error" not in state.fault_note

    def test_the_lite_status_payload_carries_both(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        from kiln import server
        from kiln.printers.base import (
            JobProgress,
            describe_fault_remedy,
            describe_unacknowledged_fault,
        )

        # The adapter's own state, as printer_status would receive it.
        fault = read_bambu_fault(CUTTER_FAULT_RENDERED, kind="print_error")
        state = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            print_error=CUTTER_FAULT_DECIMAL,
            fault_note=describe_unacknowledged_fault(CUTTER_FAULT_RENDERED, fault.reading),
            fault_remedy=describe_fault_remedy(fault.remedy),
        )
        adapter = mock.MagicMock()
        adapter.get_state.return_value = state
        adapter.get_job.return_value = JobProgress()

        with mock.patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status(detail="lite")

        assert PRIVATE_ROW["cause"] in out["printer"]["fault_note"]
        assert out["printer"]["fault_remedy"].startswith(PRIVATE_ROW["fix"])
        # Mirrored where an agent scans for warnings.
        assert out["fault_warning"] == out["printer"]["fault_note"]

    def test_a_filament_op_reports_the_cause_and_the_fix(
        self,
        adapter: BambuAdapter,
        monkeypatch: pytest.MonkeyPatch,
        bridge_answers: list[tuple[str, str]],
    ) -> None:
        """The load's own fault result: the same reading, the same fix."""
        adapter._fw_modules_requested = True
        adapter._last_status = {
            "ams": {
                "ams_exist_bits": "1",
                "tray_exist_bits": "f",
                "tray_now": "255",
                "ams": [{"id": 0, "tray": [
                    {"id": 0, "tray_type": "PLA", "nozzle_temp_min": "190", "nozzle_temp_max": "230"},
                ]}],
            },
            "gcode_state": "IDLE",
            "print_error": 0,
            "nozzle_temper": 25.0,
            "nozzle_target_temper": 0,
        }
        adapter._last_state_time = float("inf")

        def _sleep(_s: float) -> None:
            adapter._last_status["print_error"] = CUTTER_FAULT_DECIMAL

        monkeypatch.setattr(time, "sleep", _sleep)
        counter = itertools.count(0.0, 0.5)
        monkeypatch.setattr(time, "monotonic", lambda: next(counter))

        result = adapter.load_filament(slot=0)

        assert result.success is False
        assert result.error_code == "1200_8001"
        assert result.error_hint == PRIVATE_ROW["cause"]
        assert PRIVATE_ROW["fix"] in result.message
        assert result.details["remedy"] == PRIVATE_ROW["fix"]

    def test_the_fault_event_carries_the_reading_and_the_remedy(
        self, adapter: BambuAdapter, bridge_answers: list[tuple[str, str]]
    ) -> None:
        published: list[Any] = []
        bus = mock.MagicMock()
        bus.publish.side_effect = published.append

        with mock.patch("kiln.server._get_event_bus", return_value=bus):
            _push(adapter, gcode_state="idle", print_error=CUTTER_FAULT_DECIMAL)
            _join_fault_notices()

        assert len(published) == 1
        data = published[0].data
        assert data["print_error_code"] == CUTTER_FAULT_RENDERED
        assert data["reading"] == PRIVATE_ROW["cause"]
        assert data["remedy"] == PRIVATE_ROW["fix"]

    def test_troubleshoot_printer_attaches_the_decoded_block(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        """The same field the hosted boundary attaches, from the same row."""
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1", hms_code=CUTTER_FAULT_RENDERED)

        assert out["hms_code"] == "1200_8001"
        assert out["hms_decoded"] == PRIVATE_ROW
        assert "hms_wiki_url" not in out

    def test_the_four_doors_agree(
        self,
        adapter: BambuAdapter,
        bridge_answers: list[tuple[str, str]],
    ) -> None:
        """One reading, however it is reached."""
        import kiln.server as srv

        _push(adapter, gcode_state="idle", print_error=CUTTER_FAULT_DECIMAL)
        status_note = adapter.get_state().fault_note
        reading, _url = describe_bambu_filament_fault(
            CUTTER_FAULT_RENDERED, kind="print_error"
        )
        decoded = srv.troubleshoot_printer(
            "bambu_a1", hms_code=CUTTER_FAULT_RENDERED
        )["hms_decoded"]

        assert reading == decoded["cause"]
        assert reading in status_note


# ---------------------------------------------------------------------------
# 3. The bridge's own contract
# ---------------------------------------------------------------------------


class TestTheBridge:
    """The real bridge function, past the conftest's default silence."""

    def _install_fake_catalog(self, monkeypatch: pytest.MonkeyPatch, catalog: Any) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "kiln_pro", mock.MagicMock())
        monkeypatch.setitem(sys.modules, "kiln_pro.device_intelligence", mock.MagicMock())
        monkeypatch.setitem(sys.modules, "kiln_pro.device_intelligence.hms_catalog", catalog)

    def test_no_kiln_pro_reads_as_no_reading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _no_pro(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("kiln_pro"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_pro)
        bridge = _fresh_bridge()

        assert bridge.decode_fault("1200-8001", kind="print_error") is None
        assert bridge.available() is False

    def test_the_catalog_is_asked_in_the_codes_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = mock.MagicMock()
        catalog.decode_for_caller.return_value = dict(PRIVATE_ROW)
        self._install_fake_catalog(monkeypatch, catalog)

        out = _fresh_bridge().decode_fault("1200-8001", kind="print_error")

        assert out == PRIVATE_ROW
        catalog.decode_for_caller.assert_called_once_with("1200-8001", namespace="print_error")

    def test_a_row_without_a_cause_is_not_a_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = mock.MagicMock()
        catalog.decode_for_caller.return_value = {"title": "only a title"}
        self._install_fake_catalog(monkeypatch, catalog)

        assert _fresh_bridge().decode_fault("1200-8001", kind="print_error") is None

    def test_a_raising_catalog_reads_as_no_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalog = mock.MagicMock()
        catalog.decode_for_caller.side_effect = RuntimeError("catalog down")
        self._install_fake_catalog(monkeypatch, catalog)

        assert _fresh_bridge().decode_fault("1200-8001", kind="print_error") is None

    def test_an_empty_code_is_never_looked_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        catalog = mock.MagicMock()
        self._install_fake_catalog(monkeypatch, catalog)

        assert _fresh_bridge().decode_fault("", kind="print_error") is None
        catalog.decode_for_caller.assert_not_called()


def _fresh_bridge() -> Any:
    """``kiln._pro_fault_bridge`` as written, past the conftest's silence.

    The conftest patches the imported module's ``decode_fault`` so every
    other test reads public Kiln's own floor; these tests are about the
    function itself, so they load a fresh copy of the module from its file.
    """
    import importlib.util

    source = importlib.import_module("kiln._pro_fault_bridge")
    spec = importlib.util.spec_from_file_location("_fault_bridge_fresh", source.__file__)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh
