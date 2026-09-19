"""A fault is reported the way the printer's own screen reports it.

Measured on a Bambu A1 (2026-09-16, night).  The filament cutter failed
twice -- once under a paused print, once at step 3 of the printer's own
Unload -- and each time the screen showed ``[1200-8001 290420]`` (then
``[1200-8001 390434]``) over the sentence "Cutting the filament failed.
Please check to see if the cutter is stuck. Refer to the Assistant for
solutions."  Kiln showed ``print_error_code: "1200-8001"`` and nothing
else: no sentence, no sign of any further code, and a person at the screen
could not tell whether Kiln was looking at the same fault.

What the two halves of the screen are, verified against the vendor's own
client (BambuStudio, GitHub master on 2026-09-17):

* The code.  ``print_error`` is one integer on the wire (``DeviceManager.cpp``
  parses it with ``jj["print_error"].get<int>()`` and nothing beside it but
  a snapshot id under ``err2.img_id``).  The screen's ``1200-8001`` is
  ``get_error_code_str``: ``%08X`` with a dash after the fourth digit --
  ``format_error_code`` already produces exactly that.  An ``hms`` array
  entry is ``attr`` then ``code``, eight hex digits each
  (``DevHMSItem::get_long_error_code``), shown in four groups.
* The six digits after it.  Not on the wire.  BambuStudio stamps the wall
  clock into that position at display time (``DeviceErrorDialog.cpp``:
  ``wxDateTime::Now().Format("%H%M%d")``, ``"[%S %S]"``), and two
  occurrences of one code on one night carried two different numbers,
  which is what a clock stamp does and a sub-code does not.  So Kiln does
  not show one, and does not invent one.
* The sentence.  Not on the wire either.  The vendor's client asks Bambu's
  HMS text service for a per-device-type table (``HMS.cpp``,
  ``query.php?lang=en&d=<serial prefix>``; rows ``{"ecode", "intro"}``) and
  keeps a copy on disk.  The service answers without credentials, and its
  row for ``12008001`` on device type ``039`` (the A1) is the sentence
  above, word for word.  Kiln asks the same service the same way, never
  ships the table, names the source beside every sentence, and leaves the
  field ABSENT where the vendor has nothing.

The ``hms`` entries below are the SHAPE the existing filament tests use
(``{"attr": 0x12002000, "code": 0x00020006}``); tonight's report was not
captured with its ``hms`` array, so no test here claims one was.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln.printers.base import JobProgress, PrinterState, PrinterStatus

HOST = "192.0.2.10"
ACCESS_CODE = "12345678"
#: An A1-shaped serial: the vendor keys its tables by the first three.
SERIAL = "039TEST34567890"
DEVICE_TYPE = "039"

#: Tonight's numbers.  302022657 == 0x12008001.
MEASURED_PRINT_ERROR = 302022657
MEASURED_CODE = "1200-8001"
#: The two clock stamps the screen showed beside the one code.
MEASURED_STAMPS = ("290420", "390434")
#: The vendor's sentence for the code on device type 039, as served on
#: 2026-09-17 (table version 202609171044) and as the screen showed it.
VENDOR_SENTENCE = (
    "Cutting the filament failed. Please check to see if the cutter is "
    "stuck. Refer to the Assistant for solutions."
)

#: The status dict as the A1 pushed it under the paused print, and again
#: when its own Unload failed -- the same code both times.
TONIGHT_PAUSED: dict[str, Any] = {
    "gcode_state": "PAUSE",
    "print_error": MEASURED_PRINT_ERROR,
}
TONIGHT_UNLOAD_FAILED: dict[str, Any] = {
    "gcode_state": "FAILED",
    "print_error": MEASURED_PRINT_ERROR,
}
#: The same, with an ``hms`` entry in the shape the existing tests use.
HMS_SHAPE_ENTRY = {"attr": 0x12008000, "code": 0x00020001}
HMS_SHAPE_CODE = "1200-8000-0002-0001"
WITH_AN_HMS_ENTRY: dict[str, Any] = {**TONIGHT_PAUSED, "hms": [HMS_SHAPE_ENTRY]}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """A Bambu adapter with a mocked-connected MQTT client."""
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(host=HOST, access_code=ACCESS_CODE, serial=SERIAL, timeout=2)
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    publish_result.rc = mqtt.MQTT_ERR_SUCCESS
    a._mqtt_client.publish.return_value = publish_result
    a._confirm_window_s = 0.0
    return a


def _push(adapter: Any, **fields: Any) -> None:
    """Feed *fields* in as a real ``push_status`` message."""
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _seed_vendor_table(
    device_type: str = DEVICE_TYPE,
    *,
    device_error: dict[str, str] | None = None,
    device_hms: dict[str, str] | None = None,
    fetched_at: float | None = None,
) -> Path:
    """Put a table on disk where the lookup keeps the vendor's copy.

    Written, not injected: the disk file is the vendor-client contract, and
    a test that only poked the in-memory dict would pass with the disk
    reader broken.  ``HOME`` is the suite's relocated one (conftest).
    """
    from kiln.printers import bambu_hms_text

    table = {
        "device_type": device_type,
        "ver": 202609171044,
        "fetched_at": time.time() if fetched_at is None else fetched_at,
        "device_error": dict(device_error or {}),
        "device_hms": dict(device_hms or {}),
    }
    path = bambu_hms_text._cache_path(device_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table))
    bambu_hms_text._reset_for_tests()
    return path


def _join_fault_notices(timeout: float = 5.0) -> None:
    for t in threading.enumerate():
        if t.name == "kiln-fault-notice":
            t.join(timeout)


def _status_doors_adapter(state: PrinterState) -> mock.MagicMock:
    adapter = mock.MagicMock()
    adapter.get_state.return_value = state
    adapter.get_status.side_effect = AttributeError
    adapter.get_job.return_value = JobProgress()
    adapter.capabilities.to_dict.return_value = {}
    return adapter


# ---------------------------------------------------------------------------
# 1. The code, spelled as the screen spells it
# ---------------------------------------------------------------------------


class TestTheCodeInTheScreensSpelling:
    def test_tonights_print_error_renders_as_the_screen_showed_it(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults

        faults = compose_bambu_faults(TONIGHT_PAUSED)

        assert [f["code"] for f in faults] == [MEASURED_CODE]
        assert faults[0]["kind"] == "print_error"
        # Exactly what the firmware sent, under the wire's own name.
        assert faults[0]["raw"] == {"print_error": MEASURED_PRINT_ERROR}

    def test_an_hms_entry_renders_in_four_groups_with_dashes(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults, format_bambu_hms_code

        assert format_bambu_hms_code(0x12008000, 0x00020001) == HMS_SHAPE_CODE

        faults = compose_bambu_faults(WITH_AN_HMS_ENTRY)

        assert [f["code"] for f in faults] == [MEASURED_CODE, HMS_SHAPE_CODE]
        assert faults[1]["kind"] == "hms"
        assert faults[1]["raw"] == HMS_SHAPE_ENTRY

    def test_the_unload_failure_reads_the_same_as_the_paused_print(self) -> None:
        """One code, two run states, one spelling."""
        from kiln.printers.bambu import compose_bambu_faults

        assert compose_bambu_faults(TONIGHT_PAUSED) == compose_bambu_faults(
            TONIGHT_UNLOAD_FAILED
        )

    def test_the_screens_trailing_number_is_not_invented(self) -> None:
        """The six digits are a clock stamp the vendor's client draws at
        display time.  They are not on the wire, so they are not here --
        and nothing else is made up to stand in for them."""
        from kiln.printers.bambu import compose_bambu_faults

        faults = compose_bambu_faults(WITH_AN_HMS_ENTRY)

        flat = json.dumps(faults)
        for stamp in MEASURED_STAMPS:
            assert stamp not in flat
        for entry in faults:
            # No sentence (no device type to ask for one) and no fix (the
            # bridge is silent): the code, its namespace, the wire's own
            # fields, and Kiln's reading of it -- which is never empty and
            # never a stand-in for the stamp.
            assert set(entry) == {"code", "kind", "raw", "reading"}
            assert entry["reading"]

    def test_no_fault_composes_nothing(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults

        assert compose_bambu_faults({"gcode_state": "IDLE", "print_error": 0}) == []
        assert compose_bambu_faults({"gcode_state": "IDLE", "hms": []}) == []

    def test_garbage_in_the_hms_array_is_skipped_not_rendered(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults

        faults = compose_bambu_faults(
            {
                "print_error": 0,
                "hms": ["nope", {"attr": "x", "code": 1}, {"attr": 0, "code": 0}, HMS_SHAPE_ENTRY],
            }
        )

        assert [f["code"] for f in faults] == [HMS_SHAPE_CODE]


# ---------------------------------------------------------------------------
# 2. The sentence, from the vendor, with its source -- or nothing
# ---------------------------------------------------------------------------


class TestTheVendorsSentence:
    def test_the_sentence_appears_under_screen_text_with_its_source(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults
        from kiln.printers.bambu_hms_text import BAMBU_HMS_TEXT_SOURCE

        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})

        faults = compose_bambu_faults(TONIGHT_PAUSED, device_type=DEVICE_TYPE)

        assert faults[0]["screen_text"] == VENDOR_SENTENCE
        assert faults[0]["source"] == BAMBU_HMS_TEXT_SOURCE

    def test_an_hms_sentence_comes_from_the_hms_table(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults

        _seed_vendor_table(
            device_error={"12008001": VENDOR_SENTENCE},
            device_hms={"1200800000020001": "AMS lite slot 1 filament may be tangled or stuck."},
        )

        faults = compose_bambu_faults(WITH_AN_HMS_ENTRY, device_type=DEVICE_TYPE)

        assert faults[1]["screen_text"].startswith("AMS lite slot 1")

    def test_no_sentence_on_record_means_no_field_not_a_guess(self) -> None:
        """A family reading dressed as the vendor's words would be a lie
        with a citation.  The field is absent, and so is the source."""
        from kiln.printers.bambu import compose_bambu_faults

        _seed_vendor_table(device_error={"12008007": "some other row"})

        faults = compose_bambu_faults(WITH_AN_HMS_ENTRY, device_type=DEVICE_TYPE)

        for entry in faults:
            assert "screen_text" not in entry
            assert "source" not in entry

    def test_no_device_type_means_no_lookup_and_still_the_code(self) -> None:
        from kiln.printers.bambu import compose_bambu_faults

        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})

        faults = compose_bambu_faults(TONIGHT_PAUSED, device_type="")

        assert faults[0]["code"] == MEASURED_CODE
        assert "screen_text" not in faults[0]

    def test_a_code_the_vendor_does_not_list_gets_nothing_not_a_neighbour(self) -> None:
        from kiln.printers.bambu_hms_text import lookup_screen_text

        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})

        assert lookup_screen_text("1200-8002", device_type=DEVICE_TYPE, kind="print_error") is None
        # The right digits in the wrong namespace find nothing either.
        assert lookup_screen_text("1200-8001", device_type=DEVICE_TYPE, kind="hms") is None
        # And a spelling that is not a code at all.
        assert lookup_screen_text("1200", device_type=DEVICE_TYPE, kind="print_error") is None
        assert lookup_screen_text(None, device_type=DEVICE_TYPE, kind="print_error") is None

    @pytest.mark.parametrize("spelling", ["1200-8001", "1200_8001", "12008001", "1200 8001"])
    def test_every_spelling_of_the_code_finds_the_one_row(self, spelling: str) -> None:
        from kiln.printers.bambu_hms_text import lookup_screen_text

        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})

        found = lookup_screen_text(spelling, device_type=DEVICE_TYPE, kind="print_error")

        assert found is not None and found[0] == VENDOR_SENTENCE


# ---------------------------------------------------------------------------
# 3. The lookup itself: the vendor's shape, the disk, and never blocking
# ---------------------------------------------------------------------------


class TestTheLookupPlumbing:
    def test_the_device_type_is_the_serials_first_three(self) -> None:
        from kiln.printers.bambu_hms_text import device_type_from_serial

        assert device_type_from_serial("039A1B2C3D4E5F6") == "039"
        assert device_type_from_serial("00m1234") == "00M"
        assert device_type_from_serial("03") == ""
        assert device_type_from_serial(None) == ""

    def test_the_vendors_answer_compacts_to_two_tables(self) -> None:
        """The shape the service served on 2026-09-17, cut to two rows."""
        from kiln.printers.bambu_hms_text import _compact

        payload = {
            "result": 0,
            "t": 1789630254,
            "ver": 202609171044,
            "data": {
                "device_hms": {
                    "ver": 202609171044,
                    "en": [{"ecode": "1200800000020001", "intro": "AMS lite slot 1 filament may be tangled or stuck."}],
                },
                "device_error": {
                    "ver": 202609171044,
                    "en": [{"ecode": "12008001", "intro": VENDOR_SENTENCE}, {"ecode": "bad"}],
                },
            },
        }

        table = _compact(payload, "039")

        assert table is not None
        assert table["device_error"] == {"12008001": VENDOR_SENTENCE}
        assert table["device_hms"] == {"1200800000020001": "AMS lite slot 1 filament may be tangled or stuck."}
        assert table["ver"] == 202609171044
        assert table["device_type"] == "039"

    def test_the_vendors_no_such_type_answer_is_no_table(self) -> None:
        """``{"result": 201, "ver": 0}`` is what an unknown type gets."""
        from kiln.printers.bambu_hms_text import _compact

        assert _compact({"result": 201, "t": 1, "ver": 0, "msg": ""}, "ZZZ") is None
        assert _compact({"result": 0, "data": {"device_error": {"en": "not a list"}}}, "039") is None
        assert _compact("html error page", "039") is None

    def test_the_table_is_read_from_disk_where_the_vendors_client_keeps_it(self) -> None:
        from kiln.printers import bambu_hms_text

        path = _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})

        assert path == Path.home() / ".kiln" / "bambu_hms" / f"hms_en_{DEVICE_TYPE}.json"
        found = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )
        assert found is not None and found[0] == VENDOR_SENTENCE

    def test_a_corrupt_file_is_no_table_not_a_crash(self) -> None:
        from kiln.printers import bambu_hms_text

        path = _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})
        path.write_text("{not json")
        bambu_hms_text._reset_for_tests()

        assert bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        ) is None

    def test_a_missing_table_asks_the_vendor_once_in_the_background(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiln.printers import bambu_hms_text

        asked: list[str] = []
        gate = threading.Event()

        def _slow_fetch(device_type: str) -> None:
            asked.append(device_type)
            gate.wait(5.0)
            return None

        monkeypatch.setattr(bambu_hms_text, "_fetch_table", _slow_fetch)
        bambu_hms_text._reset_for_tests()

        started = time.monotonic()
        first = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )
        second = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )
        elapsed = time.monotonic() - started
        gate.set()

        # Neither read waited on the network...
        assert first is None and second is None
        assert elapsed < 1.0, f"a status-path lookup blocked for {elapsed:.2f}s"
        # ...and the two reads collapsed into one request.
        for t in threading.enumerate():
            if t.name.startswith("kiln-bambu-hms-text-"):
                t.join(5.0)
        assert asked == [DEVICE_TYPE]

    def test_a_fresh_answer_lands_on_disk_and_in_the_next_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiln.printers import bambu_hms_text

        monkeypatch.setattr(
            bambu_hms_text,
            "_fetch_table",
            lambda device_type: {
                "device_type": device_type,
                "ver": 1,
                "fetched_at": time.time(),
                "device_error": {"12008001": VENDOR_SENTENCE},
                "device_hms": {},
            },
        )
        bambu_hms_text._reset_for_tests()

        assert bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        ) is None
        for t in threading.enumerate():
            if t.name.startswith("kiln-bambu-hms-text-"):
                t.join(5.0)

        found = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )
        assert found is not None and found[0] == VENDOR_SENTENCE
        assert bambu_hms_text._cache_path(DEVICE_TYPE).exists()

    def test_an_expired_table_still_answers_and_asks_for_a_fresh_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiln.printers import bambu_hms_text

        asked: list[str] = []
        monkeypatch.setattr(
            bambu_hms_text, "_fetch_table", lambda d: asked.append(d) or None
        )
        _seed_vendor_table(
            device_error={"12008001": VENDOR_SENTENCE},
            fetched_at=time.time() - 3 * 24 * 3600,
        )

        found = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )
        for t in threading.enumerate():
            if t.name.startswith("kiln-bambu-hms-text-"):
                t.join(5.0)

        # Yesterday's sentence is still the vendor's sentence.
        assert found is not None and found[0] == VENDOR_SENTENCE
        assert asked == [DEVICE_TYPE]

    @pytest.mark.parametrize("var", ["KILN_OFFLINE", "KILN_NO_BAMBU_HMS_TEXT"])
    def test_offline_reads_the_disk_and_never_asks(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        from kiln.printers import bambu_hms_text

        asked: list[str] = []
        monkeypatch.setattr(
            bambu_hms_text, "_fetch_table", lambda d: asked.append(d) or None
        )
        monkeypatch.setenv(var, "1")
        _seed_vendor_table(
            device_error={"12008001": VENDOR_SENTENCE},
            fetched_at=time.time() - 3 * 24 * 3600,
        )

        assert bambu_hms_text.kick_background_refresh(DEVICE_TYPE) is False
        found = bambu_hms_text.lookup_screen_text(
            MEASURED_CODE, device_type=DEVICE_TYPE, kind="print_error"
        )

        assert found is not None and found[0] == VENDOR_SENTENCE
        assert asked == []

    def test_the_request_names_the_device_type_and_no_credentials(self) -> None:
        """The vendor's client sends ``lang`` and the serial's first three,
        and nothing that identifies the unit or the account."""
        from kiln.printers.bambu_hms_text import _QUERY_URL

        url = _QUERY_URL.format(device_type="039")

        assert url == "https://e.bambulab.com/query.php?lang=en&d=039"


# ---------------------------------------------------------------------------
# 4. Every door
# ---------------------------------------------------------------------------


class TestEveryDoor:
    """A fix at one door is how the bug survives at the others."""

    def _tonight(self, adapter: Any) -> PrinterState:
        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})
        _push(adapter, **WITH_AN_HMS_ENTRY)
        return adapter.get_state()

    def test_the_adapter_state_carries_the_faults(self, adapter: Any) -> None:
        state = self._tonight(adapter)

        assert state.state is PrinterStatus.ERROR
        assert state.print_error_code == MEASURED_CODE
        assert [f["code"] for f in state.faults] == [MEASURED_CODE, HMS_SHAPE_CODE]
        assert state.faults[0]["screen_text"] == VENDOR_SENTENCE

    @pytest.mark.parametrize("detail", ["full", "lite"])
    def test_printer_status_shows_the_screens_code_and_sentence(
        self, adapter: Any, detail: str
    ) -> None:
        from kiln import server

        state = self._tonight(adapter)
        with mock.patch("kiln.server._get_adapter", return_value=_status_doors_adapter(state)):
            out = server.printer_status(detail=detail)

        printer = out["printer"]
        assert printer["state"] == "error"
        assert printer["print_error_code"] == MEASURED_CODE
        faults = printer["faults"]
        assert faults[0]["code"] == MEASURED_CODE
        assert faults[0]["screen_text"] == VENDOR_SENTENCE
        assert faults[0]["source"] == "bambu_hms_service"
        assert faults[1]["code"] == HMS_SHAPE_CODE
        assert faults[1]["raw"] == HMS_SHAPE_ENTRY
        # And nothing pretends to be the screen's clock stamp.
        for stamp in MEASURED_STAMPS:
            assert stamp not in json.dumps(out)

    def test_the_monitor_report_carries_the_screen_line(self, adapter: Any) -> None:
        from kiln import server

        state = self._tonight(adapter)
        with mock.patch("kiln.server._get_adapter", return_value=_status_doors_adapter(state)):
            text = server.monitor_print(include_snapshot=False)

        assert f"Screen: {MEASURED_CODE}: {VENDOR_SENTENCE}" in text
        assert HMS_SHAPE_CODE in text

    def test_the_cli_prints_the_screen_line_under_the_fault(self, adapter: Any) -> None:
        from kiln.cli.output import format_status

        state = self._tonight(adapter)
        text = format_status(state.to_dict(), {}, json_mode=False)

        assert "Screen" in text
        assert MEASURED_CODE in text
        assert "Cutting the filament failed" in text
        assert HMS_SHAPE_CODE in text

    def test_the_cli_json_carries_the_faults_block(self, adapter: Any) -> None:
        from kiln.cli.output import format_status

        state = self._tonight(adapter)
        data = json.loads(format_status(state.to_dict(), {}, json_mode=True))["data"]

        assert data["printer"]["faults"][0]["screen_text"] == VENDOR_SENTENCE

    def test_the_fault_notice_carries_the_sentence(self, adapter: Any) -> None:
        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})
        published: list[Any] = []
        bus = mock.MagicMock()
        bus.publish.side_effect = published.append

        with mock.patch("kiln.server._get_event_bus", return_value=bus):
            _push(adapter, gcode_state="idle", print_error=0)
            _push(adapter, **TONIGHT_PAUSED)
            _join_fault_notices()

        assert len(published) == 1
        data = published[0].data
        assert data["print_error_code"] == MEASURED_CODE
        assert data["screen_text"] == VENDOR_SENTENCE
        assert data["source"] == "bambu_hms_service"
        assert data["faults"][0]["code"] == MEASURED_CODE

    def test_a_failed_filament_op_names_the_screen_code_and_sentence(
        self, adapter: Any
    ) -> None:
        from kiln.printers.base import FilamentOpPlan

        _seed_vendor_table(device_error={"12008001": VENDOR_SENTENCE})
        plan = FilamentOpPlan(action="unload", temperature=220.0, temperature_source="test")

        result = adapter._fault_result(plan, {("1200_8001", "print_error")}, stage="cut")

        assert result.success is False
        assert result.details["screen_code"] == MEASURED_CODE
        assert result.details["screen_text"] == VENDOR_SENTENCE
        assert result.details["source"] == "bambu_hms_service"

    def test_a_healthy_printer_carries_no_faults_block(self, adapter: Any) -> None:
        _push(adapter, gcode_state="IDLE", print_error=0, hms=[])
        data = adapter.get_state().to_dict()

        assert "faults" not in data

    def test_an_adapter_that_composed_nothing_claims_nothing(self) -> None:
        """``None``, not ``[]``: a firmware Kiln never asked has not said
        "no faults"."""
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)

        assert state.faults is None
        assert "faults" not in state.to_dict()

    def test_the_screen_lines_are_one_rule_for_every_text_door(self) -> None:
        from kiln.printers.base import describe_screen_faults

        lines = describe_screen_faults(
            [
                {"code": MEASURED_CODE, "kind": "print_error", "raw": {}, "screen_text": VENDOR_SENTENCE},
                {"code": HMS_SHAPE_CODE, "kind": "hms", "raw": {}},
                {"code": "0500-C010", "kind": "print_error", "raw": {}},
            ]
        )

        # A sentence on record reads code-colon-sentence; a bare HMS code is
        # still named; a bare print_error is not, because fault_note already
        # names it.
        assert lines == [f"{MEASURED_CODE}: {VENDOR_SENTENCE}", HMS_SHAPE_CODE]
        assert describe_screen_faults(None) == []
        assert describe_screen_faults("nonsense") == []

    def test_every_bambu_status_read_warms_the_table(
        self, adapter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Warmed on a healthy reading, so the sentence is on disk BEFORE the
        first fault -- not fetched in the moment it is needed."""
        from kiln.printers import bambu_hms_text

        asked: list[str] = []
        monkeypatch.setattr(
            bambu_hms_text, "_fetch_table", lambda d: asked.append(d) or None
        )
        bambu_hms_text._reset_for_tests()

        _push(adapter, gcode_state="IDLE", print_error=0)
        adapter.get_state()
        for t in threading.enumerate():
            if t.name.startswith("kiln-bambu-hms-text-"):
                t.join(5.0)

        assert asked == [DEVICE_TYPE]
