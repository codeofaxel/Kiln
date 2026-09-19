"""Every code a door hears gets the one reading, at that door.

Read off a Bambu A1 on 2026-09-19: a print cancelled mid-start-sequence,
days after a hotend replacement and toolhead reassembly, left the printer
holding two codes -- print_error ``0300-4000`` ("Z axis homing failed") on
the screen, and HMS ``0300-1800-0001-0003`` ("The extruder eddy current
sensor is not responding") that only Kiln's read showed.  kiln-pro holds
the reading for both, free.  Two public doors did not carry it:

* ``printer_status`` listed the HMS code with the vendor's sentence and
  nothing else.  The cause and the fix were one tool away, and nothing on
  the screen where the fault is MET said so.  Now every entry in
  ``faults[]`` carries ``reading`` (never empty: kiln-pro's cause when the
  caller is entitled, public Kiln's family line otherwise) and ``remedy``
  (only when Kiln has a fix) -- the same two fields, whichever namespace
  the code came out of -- and the headline is derived from that entry
  rather than read a second time.

* ``troubleshoot_printer`` heard both codes in the agent's symptom
  sentence and ignored them.  The agent is the caller here: it read two
  codes off ``printer_status``, had one ``hms_code`` slot, and put them in
  the sentence, as the tool's own description invites.  Named codes were
  matched against the playbook only, never read, and when no playbook
  carried them the answer fell through to word matches with nothing
  saying so.  Now every code the call names -- in ``hms_code`` or in the
  sentence -- is read once through the reader every other door uses
  (``fault_readings``), and codes no playbook carries are named
  (``codes_without_a_playbook``) instead of silently word-matched.

The public floor line for ``0300-4000`` is the third piece: what kind of
fault it is and where the reading is, the same pattern as the cutter code.

kiln-pro is stood in for by a stub, as in ``test_fault_reading_doors.py``:
a public test must not depend on which kiln-pro is on the path.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

import kiln._pro_fault_bridge as fault_bridge
from kiln.printers.bambu import (
    _BAMBU_PRINT_ERROR_FAULTS,
    _HMS_NAMESPACE_COLLISIONS,
    BambuAdapter,
    compose_bambu_faults,
    describe_bambu_filament_fault,
    read_bambu_fault,
)
from kiln.printers.base import PrinterStatus, describe_screen_faults

#: The two codes, as the wire carries them and as the screen spells them.
HOMING_DECIMAL = 50348032  # 0x03004000
HOMING_SCREEN = "0300-4000"
SENSOR_HMS_ENTRY = {"attr": 0x03001800, "code": 0x00010003}
SENSOR_SCREEN = "0300-1800-0001-0003"

#: Stand-ins for kiln-pro's two rows, in the shape its catalog serves.  The
#: real rows are pinned in kiln-pro; the words here are chosen so a test can
#: tell each from the public family line and from each other.
SENSOR_ROW = {
    "title": "Extruder eddy current sensor not responding",
    "cause": "The toolhead went quiet: the sensor's data over the Type-C cable never arrived.",
    "fix": "Power the printer off and unplug it, reseat the Type-C cable at both ends, then press Home.",
    "severity": "critical",
    "namespace": "hms",
}
HOMING_ROW = {
    "title": "Z axis homing failed",
    "cause": "Z homing did not complete: a cancel in the homing window, or the sensor beside it.",
    "fix": "Wait a minute, clear it, press Home; with an eddy-sensor HMS beside it fix that first.",
    "severity": "warning",
    "namespace": "print_error",
}

#: What the agent typed into the symptom door that night, in substance.
TODAYS_SYMPTOM = (
    "Print cancelled during its start sequence. The screen shows 0300-4000 "
    "Z axis homing failed; Kiln read HMS 0300-1800-0001-0003 extruder eddy "
    "current sensor not responding. The hotend was replaced and the toolhead "
    "reassembled earlier this week."
)


@pytest.fixture
def bridge_answers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """kiln-pro present: the bridge answers for the two codes, in their own
    namespaces, and records every question it was asked."""
    asked: list[tuple[str, str]] = []

    def _decode(code: str, *, kind: str = "print_error") -> dict[str, Any] | None:
        asked.append((code, kind))
        digits = "".join(c for c in code.split()[0].upper() if c in "0123456789ABCDEF")
        if kind == "hms" and digits == "0300180000010003":
            return dict(SENSOR_ROW)
        if kind == "print_error" and digits == "03004000":
            return dict(HOMING_ROW)
        return None

    monkeypatch.setattr(fault_bridge, "decode_fault", _decode)
    return asked


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BambuAdapter:
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    monkeypatch.setenv("KILN_NO_BAMBU_HMS_TEXT", "1")
    a = BambuAdapter(
        host="192.0.2.10", access_code="12345678", serial="039TEST1234567", timeout=2
    )
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    publish_result.rc = mqtt.MQTT_ERR_SUCCESS
    a._mqtt_client.publish.return_value = publish_result
    return a


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _join_fault_notices(timeout: float = 5.0) -> None:
    import threading

    for t in threading.enumerate():
        if t.name == "kiln-fault-notice":
            t.join(timeout)


#: A playbook shaped like the A1 overlay: one mode that claims a code, and
#: generic ones that share words with everything.  Installed as a fake
#: kiln-pro overlay the way test_troubleshoot_signals.py does it.
PLAYBOOK = {
    "bambu_a1": {
        "failure_modes": [
            {
                "symptom": "Repeat clogs after a new nozzle -- heat creep",
                "cause": "Heat creeping up the hotend.",
                "fix": "Re-seat the hotend; check the fan.",
            },
            {
                "symptom": "Extruder gears slipping on the filament after the toolhead was apart",
                "cause": "Tension arm not latched.",
                "fix": "Latch the arm.",
            },
            {
                "symptom": "Cutting the filament failed (1200-8001)",
                "cause": "The blade is outside its slot.",
                "fix": "Power off; reseat the blade.",
                "codes": ["1200-8001"],
            },
        ],
    }
}


def _serve_overlay(monkeypatch: pytest.MonkeyPatch, overlay: dict) -> None:
    """Install a fake kiln-pro whose printer overlay is *overlay*.

    ``{}`` is a caller with no playbook at all -- public Kiln's own floor.
    Needed explicitly: a real kiln-pro is usually importable in this suite,
    and without this a "public-only" test silently reads paid data.
    """
    import kiln.printer_intelligence as pi

    data_overlays = types.ModuleType("kiln_pro.data_overlays")
    data_overlays.load_overlay = lambda kind: overlay
    package = types.ModuleType("kiln_pro")
    package.data_overlays = data_overlays
    monkeypatch.setitem(sys.modules, "kiln_pro", package)
    monkeypatch.setitem(sys.modules, "kiln_pro.data_overlays", data_overlays)
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


@pytest.fixture
def playbook(monkeypatch: pytest.MonkeyPatch):
    import kiln.printer_intelligence as pi

    _serve_overlay(monkeypatch, PLAYBOOK)
    yield
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


@pytest.fixture
def no_playbook(monkeypatch: pytest.MonkeyPatch):
    import kiln.printer_intelligence as pi

    _serve_overlay(monkeypatch, {})
    yield
    monkeypatch.setattr(pi, "_merged_cache", None, raising=False)


def _troubleshoot(**kwargs: Any) -> dict:
    from kiln.server import troubleshoot_printer as tool

    return getattr(tool, "fn", tool)(**kwargs)


# ---------------------------------------------------------------------------
# C. The public floor: what kind of fault, and where the reading is
# ---------------------------------------------------------------------------


class TestThePublicFloor:
    def test_the_homing_code_gets_a_kind_line_not_a_shrug(self) -> None:
        reading, url = describe_bambu_filament_fault(HOMING_SCREEN, kind="print_error")

        assert "Z-axis homing fault" in reading
        assert "free with a Kiln sign-in" in reading
        assert "publishes no page" not in reading
        assert url is None

    def test_the_floor_line_carries_none_of_the_reading(self) -> None:
        line = _BAMBU_PRINT_ERROR_FAULTS["03004000"].lower()
        for know_how in ("cable", "cancel", "eddy", "reseat", "touch", "http", "wiki"):
            assert know_how not in line, f"the floor line carries {know_how!r}"

    def test_the_collision_with_the_hms_module_is_recorded(self) -> None:
        # 0300-4000-0002-000x is serial-port / G-code data transmission in
        # the HMS namespace -- a different fault under the same eight digits.
        assert "03004000" in _HMS_NAMESPACE_COLLISIONS
        assert "serial" in _HMS_NAMESPACE_COLLISIONS["03004000"].lower()


# ---------------------------------------------------------------------------
# B. The status door: every entry carries the reading, once
# ---------------------------------------------------------------------------


class TestTheStatusDoorCarriesEveryReading:
    def test_every_entry_carries_the_reading_and_the_fix(
        self, adapter: BambuAdapter, bridge_answers: list[tuple[str, str]]
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=HOMING_DECIMAL, hms=[SENSOR_HMS_ENTRY])
        _join_fault_notices()
        bridge_answers.clear()

        state = adapter.get_state()

        assert state.state is PrinterStatus.ERROR
        by_code = {f["code"]: f for f in state.faults}
        assert by_code[HOMING_SCREEN]["kind"] == "print_error"
        assert by_code[HOMING_SCREEN]["reading"] == HOMING_ROW["cause"]
        assert by_code[HOMING_SCREEN]["remedy"] == HOMING_ROW["fix"]
        assert by_code[SENSOR_SCREEN]["kind"] == "hms"
        assert by_code[SENSOR_SCREEN]["reading"] == SENSOR_ROW["cause"]
        assert by_code[SENSOR_SCREEN]["remedy"] == SENSOR_ROW["fix"]
        # The headline is the print_error entry's own reading -- one read per
        # code, not a separate lookup for the headline.
        assert HOMING_ROW["cause"] in state.fault_note
        assert state.fault_remedy.startswith(HOMING_ROW["fix"])
        assert sorted(kind for _code, kind in bridge_answers) == ["hms", "print_error"]
        assert len(bridge_answers) == 2

    def test_a_public_only_entry_carries_the_family_line_and_no_remedy(
        self, adapter: BambuAdapter
    ) -> None:
        _push(adapter, gcode_state="idle", print_error=HOMING_DECIMAL, hms=[SENSOR_HMS_ENTRY])

        state = adapter.get_state()

        by_code = {f["code"]: f for f in state.faults}
        assert "Z-axis homing fault" in by_code[HOMING_SCREEN]["reading"]
        assert "remedy" not in by_code[HOMING_SCREEN]
        assert by_code[SENSOR_SCREEN]["reading"]
        assert "remedy" not in by_code[SENSOR_SCREEN]

    def test_the_composer_never_invents_a_remedy(self) -> None:
        faults = compose_bambu_faults({"print_error": HOMING_DECIMAL, "hms": [SENSOR_HMS_ENTRY]})
        for entry in faults:
            assert entry["reading"]
            assert "remedy" not in entry
            assert set(entry) <= {"code", "kind", "raw", "reading", "remedy", "screen_text", "source"}

    def test_the_reader_and_the_composer_agree(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        faults = compose_bambu_faults({"print_error": HOMING_DECIMAL, "hms": [SENSOR_HMS_ENTRY]})
        for entry in faults:
            fault = read_bambu_fault(entry["code"], kind=entry["kind"])
            assert entry["reading"] == fault.reading
            assert entry.get("remedy") == fault.remedy

    def test_the_text_surfaces_print_the_fix_under_an_hms_code(self) -> None:
        """The CLI and the monitor report print one line per code.  An HMS
        code's reading lives nowhere else on those surfaces, so when Kiln has
        a fix for it the reading and the fix go under the code line.  A
        print_error's reading is already the headline and is not repeated."""
        lines = describe_screen_faults(
            [
                {
                    "code": HOMING_SCREEN,
                    "kind": "print_error",
                    "screen_text": "Z axis homing failed; the task has been stopped.",
                    "reading": HOMING_ROW["cause"],
                    "remedy": HOMING_ROW["fix"],
                },
                {
                    "code": SENSOR_SCREEN,
                    "kind": "hms",
                    "screen_text": "The extruder eddy current sensor is not responding.",
                    "reading": SENSOR_ROW["cause"],
                    "remedy": SENSOR_ROW["fix"],
                },
            ]
        )
        assert lines[0].startswith(f"{HOMING_SCREEN}: Z axis homing failed")
        assert HOMING_ROW["cause"] not in " ".join(lines)
        assert lines[1].startswith(f"{SENSOR_SCREEN}: The extruder eddy current sensor")
        assert lines[2].strip() == SENSOR_ROW["cause"]
        assert lines[3].strip() == SENSOR_ROW["fix"]

    def test_the_text_surfaces_stay_quiet_without_a_fix(self) -> None:
        # A public family line is not a fix; it does not earn a second line.
        lines = describe_screen_faults(
            [{"code": SENSOR_SCREEN, "kind": "hms", "reading": "a fault Kiln has no reading for"}]
        )
        assert lines == [SENSOR_SCREEN]

    def test_the_fault_event_entries_carry_the_reading(
        self, adapter: BambuAdapter, bridge_answers: list[tuple[str, str]]
    ) -> None:
        published: list[Any] = []
        bus = mock.MagicMock()
        bus.publish.side_effect = published.append

        with mock.patch("kiln.server._get_event_bus", return_value=bus):
            _push(adapter, gcode_state="idle", print_error=HOMING_DECIMAL)
            _join_fault_notices()

        assert len(published) == 1
        entry = published[0].data["faults"][0]
        assert entry["reading"] == HOMING_ROW["cause"]
        assert entry["remedy"] == HOMING_ROW["fix"]


# ---------------------------------------------------------------------------
# A. The symptom door: every code it hears is read, and the unread are named
# ---------------------------------------------------------------------------


class TestTheSymptomDoorHearsEveryCode:
    def test_codes_in_the_sentence_are_read_through_the_reader(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        out = _troubleshoot(printer_id="bambu_a1", symptom=TODAYS_SYMPTOM)

        readings = out["fault_readings"]
        assert [r["code"] for r in readings] == [HOMING_SCREEN, SENSOR_SCREEN]
        assert [r["kind"] for r in readings] == ["print_error", "hms"]
        assert readings[0]["reading"] == HOMING_ROW["cause"]
        assert readings[0]["remedy"] == HOMING_ROW["fix"]
        assert readings[1]["reading"] == SENSOR_ROW["cause"]
        assert readings[1]["remedy"] == SENSOR_ROW["fix"]
        # Asked once per code, each in its own namespace.
        assert sorted(kind for _c, kind in bridge_answers) == ["hms", "print_error"]
        assert len(bridge_answers) == 2

    def test_the_explicit_code_and_the_named_codes_are_one_list(
        self, bridge_answers: list[tuple[str, str]]
    ) -> None:
        out = _troubleshoot(
            printer_id="bambu_a1",
            symptom=f"Z homing failed, {HOMING_SCREEN}, and again {HOMING_SCREEN}",
            hms_code=SENSOR_SCREEN,
        )
        # The explicit code leads; the sentence's codes follow, de-duplicated.
        assert [r["code"] for r in out["fault_readings"]] == [SENSOR_SCREEN, HOMING_SCREEN]
        # The legacy fields for the explicit code are untouched.
        assert out["hms_code"] == "0300_1800_0001_0003"
        assert out["hms_decoded"] == SENSOR_ROW

    def test_a_public_only_install_still_reads_the_floor_line(self, no_playbook: None) -> None:
        out = _troubleshoot(printer_id="bambu_a1", symptom=f"screen says {HOMING_SCREEN}")

        (entry,) = out["fault_readings"]
        assert entry["code"] == HOMING_SCREEN
        assert "Z-axis homing fault" in entry["reading"]
        assert "remedy" not in entry

    def test_the_readings_carry_no_link(self, bridge_answers: list[tuple[str, str]]) -> None:
        out = _troubleshoot(printer_id="bambu_a1", symptom=TODAYS_SYMPTOM)
        for entry in out["fault_readings"]:
            assert set(entry) <= {"code", "kind", "reading", "remedy"}
            assert "http" not in json.dumps(entry)

    def test_no_code_named_means_no_field(self) -> None:
        out = _troubleshoot(printer_id="bambu_a1", symptom="stringing with PETG")
        assert "fault_readings" not in out
        assert "codes_without_a_playbook" not in out

    def test_a_named_code_no_playbook_carries_is_said_out_loud(self, playbook: None) -> None:
        """The failure of 2026-09-19: both codes named, no mode carried them,
        and the answer was hot-end entries matched on words with nothing
        saying the codes went unmatched.  The word matches stay -- they are
        labelled -- but the unmatched codes are named beside them."""
        out = _troubleshoot(printer_id="bambu_a1", symptom=TODAYS_SYMPTOM)

        assert out["codes_without_a_playbook"] == [HOMING_SCREEN, SENSOR_SCREEN]
        assert out["matches"], "the word matches were dropped, not labelled"
        assert all(m["matched_on"] == "text" for m in out["matches"])

    def test_a_carried_code_is_not_listed_as_missing(self, playbook: None) -> None:
        out = _troubleshoot(printer_id="bambu_a1", symptom="the cutter failed, 1200-8001")
        assert "codes_without_a_playbook" not in out
        assert [m["matched_on"] for m in out["matches"]] == ["code"]

    def test_only_the_uncarried_codes_are_named(self, playbook: None) -> None:
        out = _troubleshoot(
            printer_id="bambu_a1", symptom=f"1200-8001 and then {HOMING_SCREEN}"
        )
        assert out["codes_without_a_playbook"] == [HOMING_SCREEN]

    def test_without_a_playbook_at_all_nothing_is_called_missing(self, no_playbook: None) -> None:
        # A public-only caller has no playbook to judge a code against; the
        # honest field there is the nudge, not a list of "missing" codes.
        out = _troubleshoot(printer_id="bambu_a1", symptom=f"screen says {HOMING_SCREEN}")
        assert "codes_without_a_playbook" not in out
        assert out["fault_readings"][0]["code"] == HOMING_SCREEN

    def test_the_nudge_does_not_deny_a_reading_it_just_attached(
        self, no_playbook: None, bridge_answers: list[tuple[str, str]]
    ) -> None:
        """A caller with no playbook but a kiln-pro reading on the wire (a
        free row, served to a signed-in caller of any tier) must not be told
        Kiln cannot read a fault code here."""
        from kiln.tiers_and_terms import reset_spoken_keys

        reset_spoken_keys()
        try:
            out = _troubleshoot(printer_id="bambu_a1", symptom=f"screen says {HOMING_SCREEN}")
        finally:
            reset_spoken_keys()

        assert out["fault_readings"][0]["remedy"] == HOMING_ROW["fix"]
        hint = out["upgrade_hint"]
        assert hint, "no playbook served, so the nudge must have fired"
        assert "can't read a fault code" not in hint
        assert "kiln3d.com" in hint

    def test_the_nudge_still_names_the_gap_when_nothing_was_read(
        self, no_playbook: None
    ) -> None:
        """The bridge silent: the floor line is public Kiln's own, so the
        specific nudge -- Kiln cannot read a fault code here -- stays true."""
        from kiln.tiers_and_terms import reset_spoken_keys

        reset_spoken_keys()
        try:
            out = _troubleshoot(printer_id="bambu_a1", symptom=f"screen says {HOMING_SCREEN}")
        finally:
            reset_spoken_keys()

        assert "remedy" not in out["fault_readings"][0]
        assert "can't read a fault code" in out["upgrade_hint"]
