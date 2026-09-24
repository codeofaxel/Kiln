"""The cutter bridge: what the printer doors report, and that reporting never hurts.

Pinned here:
  - the sliced file's own ``total filament change`` line is read (Bambu Studio
    and Orca write it in the footer); a single-colour file with no line plans
    zero changes; an unreadable file is ``None``, never zero;
  - the three reports reach the local counter when kiln-pro is importable,
    the hosted wire otherwise, and nothing when neither answers -- and none
    of them raise, whatever the counter or the network does;
  - a hosted blade-status request carries this install's recent fault codes
    along, raw, from the event log Kiln already keeps;
  - the report names no maker and no model of its own: the model it sends is
    the one the person declared for the printer.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from kiln import _pro_cutter_bridge as bridge

# ---------------------------------------------------------------------------
# the sliced file
# ---------------------------------------------------------------------------


def _gcode(tmp_path: Path, body: str, name: str = "part.gcode") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


class TestReadingTheFile:
    def test_bambus_footer_line_is_read(self, tmp_path):
        path = _gcode(tmp_path, "G28\n" * 50 + "; total filament used [g] = 0.00\n; total filament change = 167\n; filament_change_length = 10\n")
        assert bridge.planned_cuts_in_file(path) == 167

    def test_a_single_colour_file_plans_no_change(self, tmp_path):
        path = _gcode(tmp_path, "; total filament weight [g] : 3.89\n; change_filament_gcode = M620 S[next_extruder]A\nG1 X10\n")
        assert bridge.planned_cuts_in_file(path) == 0

    def test_an_unreadable_file_is_none_never_zero(self, tmp_path):
        assert bridge.planned_cuts_in_file(str(tmp_path / "missing.gcode")) is None
        assert bridge.planned_cuts_in_file("") is None
        assert bridge.planned_cuts_in_file(None) is None

    def test_the_footer_is_found_past_the_head_window(self, tmp_path):
        path = _gcode(tmp_path, "G1 X1\n" * 5000 + "; total filament change = 12\n")
        assert bridge.planned_cuts_in_file(path) == 12

    def test_an_orca_footer_further_from_the_end_is_still_read(self, tmp_path):
        # OrcaSlicer writes these about 590 lines from the end; a longer
        # settings block pushes them further.  A 600-line tail window read
        # "no changes planned, no grams" once they passed it.
        body = (
            "G1 X1 Y1 E.05\n" * 3000
            + "; filament used [g] = 32.93, 1.74\n; total filament used [g] = 34.67\n"
            + "; total filament change = 8\n"
            + "".join(f"; setting_{i} = {i}\n" for i in range(700))
        )
        path = _gcode(tmp_path, body)
        assert bridge.planned_cuts_in_file(path) == 8
        assert bridge.grams_in_file(path) == pytest.approx(34.67)

    def test_a_zero_grams_line_is_not_a_weight(self, tmp_path):
        # A zero is the slicer saying it could not work the weight out.
        path = _gcode(tmp_path, "; filament used [g] = 0.00\n; total filament used [g] = 34.67\n")
        assert bridge.grams_in_file(path) == pytest.approx(34.67)

    def test_grams_come_from_the_slicers_own_line(self, tmp_path):
        assert bridge.grams_in_file(_gcode(tmp_path, "; total filament weight [g] : 3.89\n")) == 3.89
        assert bridge.grams_in_file(_gcode(tmp_path, "; filament used [g] = 12.5, 3.5\n")) == 16.0
        assert bridge.grams_in_file(_gcode(tmp_path, "G1 X1\n")) is None


# ---------------------------------------------------------------------------
# the three reports
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, printer_id, **payload):
        self.calls.append((printer_id, payload))
        return {"recorded": payload}


@pytest.fixture
def local_counter(monkeypatch):
    """A kiln-pro counter the bridge finds locally."""
    import types

    fake = _Counter()
    pkg = types.ModuleType("kiln_pro")
    sub = types.ModuleType("kiln_pro.cutter_intelligence")
    mod = types.ModuleType("kiln_pro.cutter_intelligence.counter")
    mod.record_cut_events = fake
    monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence", sub)
    monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence.counter", mod)
    monkeypatch.setattr(bridge, "_declared_model", lambda name: "bambu_a1")
    # The path each report names is the file itself: an earlier test's
    # slice-ledger line for a same-named file must not answer for it.
    monkeypatch.setattr("kiln.monitor_twin.sliced_entry_for", lambda name: None)
    return fake


class TestTheLocalFile:
    def test_a_printer_side_name_resolves_to_the_retained_gcode(self, tmp_path, monkeypatch):
        gcode = _gcode(tmp_path, "; total filament change = 4\n", name="part.gcode")
        monkeypatch.setattr(
            "kiln.monitor_twin.sliced_entry_for",
            lambda name: {"output": gcode, "wrapped": str(tmp_path / "part.gcode.3mf")} if name == "part.gcode.3mf" else None,
        )
        assert bridge.local_sliced_path("part.gcode.3mf") == gcode
        assert bridge.local_sliced_path("never-sliced-here.3mf") is None

    def test_a_readable_path_is_taken_as_it_is(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiln.monitor_twin.sliced_entry_for", lambda name: None)
        gcode = _gcode(tmp_path, "G1 X1\n")
        assert bridge.local_sliced_path(gcode) == gcode

    def test_a_start_reports_through_the_retained_file(self, tmp_path, local_counter, monkeypatch):
        gcode = _gcode(tmp_path, "; total filament weight [g] : 8.0\n; total filament change = 3\n")
        monkeypatch.setattr("kiln.monitor_twin.sliced_entry_for", lambda name: {"output": gcode, "wrapped": None})
        bridge.record_print_cuts("a1", "part.gcode.3mf")
        (name, payload), = local_counter.calls
        assert payload["planned_cuts"] == 3 and payload["grams"] == 8.0
        assert payload["dedupe_key"].startswith("start:a1:part.gcode.3mf:")


class TestTheReports:
    def test_a_print_start_reports_planned_changes_and_grams(self, tmp_path, local_counter):
        path = _gcode(tmp_path, "; total filament weight [g] : 40.0\n; total filament change = 9\n")
        bridge.record_print_cuts("a1", path)
        (name, payload), = local_counter.calls
        assert name == "a1"
        assert {k: v for k, v in payload.items() if k != "dedupe_key"} == {"printer_model": "bambu_a1", "planned_cuts": 9, "grams": 40.0}
        assert payload["dedupe_key"].startswith(f"start:a1:{path}:")

    def test_a_single_colour_start_with_no_grams_reports_nothing(self, tmp_path, local_counter):
        bridge.record_print_cuts("a1", _gcode(tmp_path, "G1 X1\n"))
        assert local_counter.calls == []

    def test_a_command_reports_its_verb(self, local_counter):
        bridge.record_command_cut("a1", "unload")
        (name, payload), = local_counter.calls
        assert name == "a1" and payload["command"] == "unload" and payload["printer_model"] == "bambu_a1"
        assert payload["dedupe_key"].startswith("unload:a1:")

    def test_an_observed_switch_reports_one_switch(self, local_counter):
        bridge.record_observed_switch("a1", from_tray="255", to_tray="1")
        (_, payload), = local_counter.calls
        assert payload["observed_switches"] == 1
        assert "255->1" in payload["dedupe_key"]

    def test_a_declared_model_passed_in_wins_over_the_lookup(self, local_counter):
        bridge.record_command_cut("a1", "load", printer_model="bambu_p1s")
        assert local_counter.calls[0][1]["printer_model"] == "bambu_p1s"

    def test_a_crashing_local_counter_never_raises(self, monkeypatch, local_counter):
        def boom(*_a, **_k):
            raise RuntimeError("store on fire")

        sys.modules["kiln_pro.cutter_intelligence.counter"].record_cut_events = boom
        bridge.record_command_cut("a1", "load")  # no exception is the assertion

    def test_without_kiln_pro_the_report_goes_to_the_hosted_wire_off_thread(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence.counter", None)
        monkeypatch.setattr(bridge, "_declared_model", lambda name: "bambu_a1")
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        calls: list[tuple] = []

        import kiln.server as server

        def fake_call(tool, **kwargs):
            calls.append((tool, kwargs))
            return {"success": True}

        monkeypatch.setattr(server, "_pro_api_call", fake_call)
        bridge.record_command_cut("a1", "cancel")
        deadline = time.monotonic() + 2.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls and calls[0][0] == bridge.WIRE_TOOL
        assert calls[0][1]["printer_id"] == "a1" and calls[0][1]["command"] == "cancel"

    def test_an_unreachable_service_backs_off_instead_of_retrying_every_print(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.cutter_intelligence.counter", None)
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        import kiln.server as server

        monkeypatch.setattr(server, "_pro_api_call", lambda *_a, **_k: (_ for _ in ()).throw(OSError("down")))
        bridge._served_report("a1", {"command": "load"})
        assert bridge._service_down_until > time.monotonic()


# ---------------------------------------------------------------------------
# what a status request carries
# ---------------------------------------------------------------------------


class TestRecentFaults:
    def test_the_local_event_log_is_read_for_the_named_machine(self, monkeypatch):
        now = time.time()
        rows = [
            {"timestamp": now - 60, "source": "printer:a1", "data": {"printer_name": "a1", "print_error_code": "1200-8001"}},
            {"timestamp": now - 120, "source": "printer:p1", "data": {"printer_name": "p1", "print_error_code": "0300-0D00-0001-0001"}},
            {"timestamp": now - 40 * 86400, "source": "printer:a1", "data": {"printer_name": "a1", "print_error_code": "1200-8001"}},
        ]

        class _DB:
            def recent_events(self, event_type=None, limit=50):
                # The name the bus persists under: the enum's value, which
                # is "printer.error", not the underscore spelling a reader
                # would guess.  The bridge asked for the wrong name once and
                # every status request carried an empty fault list.
                from kiln.events import EventType

                assert event_type == EventType.PRINTER_ERROR.value == "printer.error"
                return rows

        monkeypatch.setattr("kiln.persistence.get_db", lambda: _DB())
        found = bridge.recent_faults_for("a1")
        assert [f["code"] for f in found] == ["1200-8001"]
        assert found[0]["at"].endswith("+00:00")

    def test_no_log_is_no_faults(self, monkeypatch):
        monkeypatch.setattr("kiln.persistence.get_db", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        assert bridge.recent_faults_for("a1") == []

    def test_a_status_request_is_enriched_and_a_supplied_list_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(bridge, "recent_faults_for", lambda name, days=30: [{"code": "X", "at": "t"}] if name == "a1" else [])
        out = bridge.with_recent_faults("cutter_wear_status", {"printer_id": "a1"})
        assert out["recent_faults"] == [{"code": "X", "at": "t"}]
        untouched = bridge.with_recent_faults("cutter_wear_status", {"printer_id": "a1", "recent_faults": []})
        assert untouched["recent_faults"] == []
        sweep = bridge.with_recent_faults("maintenance_due", {"printer_names": ["a1", "p1"]})
        assert sweep["recent_faults"] == {"a1": [{"code": "X", "at": "t"}], "p1": []}
        assert bridge.with_recent_faults("get_nozzle_state", {"printer_id": "a1"}) == {"printer_id": "a1"}


class TestTheFaultLogPairing:
    def test_a_fault_the_bus_persisted_reaches_the_blade_request(self, tmp_path, monkeypatch):
        """End to end through the real log, not a fake: the event the fault
        edge publishes, written the way the bus persists it, is what a
        status request carries.  A fake that agreed with the bridge's own
        spelling of the event name hid an empty list for a day."""
        from kiln.events import EventType
        from kiln.persistence import KilnDB

        db = KilnDB(str(tmp_path / "kiln.db"))
        db.log_event(
            event_type=EventType.PRINTER_ERROR.value,
            data={"printer_name": "a1", "print_error": 0x12008001, "print_error_code": "1200-8001"},
            source="printer:a1",
        )
        monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
        found = bridge.recent_faults_for("a1")
        assert [f["code"] for f in found] == ["1200-8001"]
        assert bridge.recent_faults_for("p1") == []
