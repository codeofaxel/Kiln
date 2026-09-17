"""The plate record: what is on the build plate, as far as Kiln can say.

Born 2026-09-16.  Every idle-printer motion Kiln sends starts with the
machine's own raise and a travel across the head's row, and a collision
there raises no fault.  The one fact that decides
whether that is safe, the height of whatever is on the plate, is the one
fact the printer cannot report.  So Kiln writes it down at the moments it
can be sure of (a print it started; a print it saw end) and a person
clears it.  The default reading is ``unknown``, which asks -- the opposite
of the engagement record, and on purpose.
"""

from __future__ import annotations

import json
import uuid
import zipfile
from types import SimpleNamespace
from unittest import mock

import pytest

from kiln.plate_state import (
    PlateJob,
    PlateState,
    _store_path,
    geometry_of,
    job_for_start,
    mark_clear,
    mark_occupied,
    mark_unknown,
    plate_occupancy,
    raise_clearance_mm,
    read,
)
from kiln.printers.base import PrinterAdapter, PrintResult

from .test_filament_handling import bambu  # noqa: F401

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name


@pytest.fixture(autouse=True)
def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))


def _machine(serial: str = "01P00A000000001") -> SimpleNamespace:
    return SimpleNamespace(name="bambu", serial=serial, _printer_model="bambu_a1")


class TestStore:
    def test_no_record_reads_as_unknown(self):
        state = read(_machine())
        assert state.status == "unknown" and not state.occupied and not state.clear
        assert "no record" in state.describe()

    def test_occupied_round_trips_with_the_job(self):
        m = _machine()
        mark_occupied(m, PlateJob(file="coaster.3mf", footprint_mm=[10, 10, 90, 90], max_z_mm=18.0, printer_id="bambu_a1"))
        state = read(m)
        assert state.occupied and state.source == "kiln_started_print"
        assert state.job == PlateJob(file="coaster.3mf", footprint_mm=[10.0, 10.0, 90.0, 90.0], max_z_mm=18.0, printer_id="bambu_a1")
        assert state.since and "coaster.3mf" in state.describe() and "18 mm tall" in state.describe()
        on_disk = json.loads(_store_path().read_text())
        assert on_disk["version"] == 1 and set(on_disk["machines"]) == {"bambu:serial:01p00a000000001"}

    def test_clear_then_unknown(self):
        m = _machine()
        mark_clear(m, "human", note="took the vase off")
        state = read(m)
        assert state.clear and state.source == "human" and state.note == "took the vase off"
        assert "a person said so" in state.describe()
        mark_unknown(m, "power cycled")
        assert read(m).status == "unknown"

    def test_a_torn_file_reads_as_unknown_not_clear(self):
        m = _machine()
        mark_clear(m, "human")
        assert read(m).clear
        path = _store_path()
        path.write_text(path.read_text()[: len(path.read_text()) // 2])  # truncated mid-write
        assert read(m).status == "unknown"

    def test_a_future_schema_or_a_hand_edit_reads_as_unknown(self):
        m = _machine()
        mark_clear(m, "human")
        data = json.loads(_store_path().read_text())
        data["version"] = 99
        _store_path().write_text(json.dumps(data))
        assert read(m).status == "unknown"
        _store_path().write_text(json.dumps({"version": 1, "machines": {"bambu:serial:01p00a000000001": {"status": "spotless"}}}))
        assert read(m).status == "unknown"

    def test_two_machines_never_share_a_plate(self):
        a, b = _machine("AAA"), _machine("BBB")
        mark_occupied(a, PlateJob(file="a.3mf", max_z_mm=30.0))
        mark_clear(b, "human")
        assert read(a).occupied and read(b).clear

    def test_an_unidentifiable_machine_is_unknown_and_writes_nothing(self):
        m = SimpleNamespace(name="octo")  # no serial, no host: only an object id
        mark_occupied(m, PlateJob(file="x.gcode"))
        state = read(m)
        assert state.status == "unknown" and "neither a serial nor an address" in state.note
        assert not _store_path().exists()

    def test_reasserting_occupied_without_a_job_keeps_the_job_on_record(self):
        m = _machine()
        mark_occupied(m, PlateJob(file="coaster.3mf", max_z_mm=18.0))
        mark_occupied(m, None, source="print_ended")
        state = read(m)
        assert state.occupied and state.source == "print_ended" and state.job.file == "coaster.3mf"
        assert state.job.max_z_mm == 18.0

    def test_a_clear_plate_reasserted_occupied_holds_an_unnamed_part(self):
        m = _machine()
        mark_clear(m, "human")
        mark_occupied(m, None, source="print_ended")
        state = read(m)
        assert state.occupied and state.job is None and "holds a part" in state.describe()

    def test_plate_occupancy_is_the_record(self):
        m = _machine()
        mark_occupied(m, PlateJob(file="coaster.3mf"))
        assert plate_occupancy(m) == read(m)

    def test_nothing_raises_into_a_caller(self, monkeypatch):
        import kiln.plate_state as ps

        monkeypatch.setattr(ps, "_read_store", mock.Mock(side_effect=RuntimeError("disk")))
        m = _machine()
        assert read(m).status == "unknown"
        mark_occupied(m, PlateJob(file="x"))
        mark_clear(m, "human")
        mark_unknown(m, "why")

    def test_to_dict_carries_the_description(self):
        state = PlateState(machine="m", status="occupied", source="kiln_started_print", since="2026-09-16T18:12:00-07:00",
                           job=PlateJob(file="coaster.3mf", max_z_mm=18.0))
        d = state.to_dict()
        assert d["status"] == "occupied" and d["job"]["max_z_mm"] == 18.0
        assert "coaster.3mf" in d["description"] and "18:12" in d["description"]


def _gcode_3mf(path, *, gcode: bytes, plate_json: dict | None) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        if plate_json is not None:
            zf.writestr("Metadata/plate_1.json", json.dumps(plate_json))
        zf.writestr("Metadata/plate_1.gcode", gcode)
    return str(path)


class TestGeometry:
    def test_prusaslicer_layer_comments_give_the_height(self, tmp_path):
        p = tmp_path / "coaster.gcode"
        p.write_bytes(b"; generated by PrusaSlicer\n;Z:0.2\nG1 X1\n;Z:4.6\n;Z:18.4\nG1 X2\n;Z:3\n")
        assert geometry_of(str(p)) == (None, 18.4)

    def test_a_genuine_bambu_export_gives_footprint_and_header_height(self, tmp_path):
        path = _gcode_3mf(tmp_path / "vase.gcode.3mf",
                          gcode=b"; HEADER_BLOCK_START\n; max_z_height: 61.20\n; HEADER_BLOCK_END\nG1 X1\n",
                          plate_json={"bbox_objects": [{"name": "a", "bbox": [10, 20, 50, 60]}, {"name": "b", "bbox": [40, 5, 90, 30]}]})
        assert geometry_of(path) == ([10.0, 5.0, 90.0, 60.0], 61.2)

    def test_a_kiln_wrapped_3mf_never_trusts_its_own_fallback_header(self, tmp_path):
        # Kiln's wrapper writes bbox_objects: [] and max_z_height 10.00 when the
        # body has no layer comments -- a placeholder, not a height.
        path = _gcode_3mf(tmp_path / "hand.gcode.3mf",
                          gcode=b"; HEADER_BLOCK_START\n; max_z_height: 10.00\n; HEADER_BLOCK_END\nG1 Z50\n",
                          plate_json={"bbox_all": [78, 78, 178, 178], "bbox_objects": []})
        assert geometry_of(path) == (None, None)

    def test_the_body_wins_over_the_header(self, tmp_path):
        path = _gcode_3mf(tmp_path / "wrap.gcode.3mf",
                          gcode=b"; max_z_height: 10.00\n;Z:0.2\n;Z:33.0\n",
                          plate_json={"bbox_objects": []})
        assert geometry_of(path) == (None, 33.0)

    def test_a_standalone_export_header_is_trusted(self, tmp_path):
        p = tmp_path / "export.gcode"
        p.write_bytes(b"; HEADER_BLOCK_START\n; max_z_height: 42.5\n; HEADER_BLOCK_END\n")
        assert geometry_of(str(p)) == (None, 42.5)

    def test_a_comment_split_across_chunks_is_still_seen(self, tmp_path, monkeypatch):
        import kiln.plate_state as ps

        monkeypatch.setattr(ps, "_SCAN_CHUNK", 8)
        p = tmp_path / "split.gcode"
        p.write_bytes(b"G1 X1\n;Z:0.2\n;Z:27.85\nG1 X2\n")
        assert geometry_of(str(p)) == (None, 27.85)

    def test_over_the_cap_reports_no_height_rather_than_a_low_one(self, tmp_path, monkeypatch):
        import kiln.plate_state as ps

        monkeypatch.setattr(ps, "_MAX_SCAN_BYTES", 16)
        p = tmp_path / "big.gcode"
        p.write_bytes(b";Z:0.2\n" + b"G1 X1\n" * 20 + b";Z:80\n")
        assert geometry_of(str(p)) == (None, None)

    def test_garbage_is_nothing(self, tmp_path):
        p = tmp_path / "x.gcode"
        p.write_bytes(b"\x00\xff")
        assert geometry_of(str(p)) == (None, None)
        assert geometry_of(str(tmp_path / "missing.gcode")) == (None, None)

    def test_job_for_start_finds_the_file_kiln_sliced_by_its_printer_side_name(self, tmp_path):
        from kiln.monitor_twin import note_sliced, note_wrapped

        tag = uuid.uuid4().hex[:8]
        mesh, out = tmp_path / f"coaster-{tag}.stl", tmp_path / f"coaster-{tag}.gcode"
        mesh.write_bytes(b"solid")
        out.write_bytes(b";Z:0.2\n;Z:18.4\n")
        note_sliced(str(mesh), str(out))
        note_wrapped(str(out), str(tmp_path / f"coaster-{tag}.gcode.3mf"))  # the printer-side name
        job = job_for_start(_machine(), f"coaster-{tag}.gcode.3mf")
        assert job == PlateJob(file=f"coaster-{tag}.gcode.3mf", footprint_mm=None, max_z_mm=18.4, printer_id="bambu_a1")

    def test_a_file_kiln_never_saw_is_a_part_of_unknown_size(self):
        job = job_for_start(_machine(), f"/sdcard/mystery-{uuid.uuid4().hex[:6]}.3mf")
        assert job.max_z_mm is None and job.footprint_mm is None and job.file.startswith("mystery-")

    def test_raise_clearance_from_the_station_record(self):
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": 40, "back_down_mm": 15}}) == 25.0
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": 40}}) is None
        assert raise_clearance_mm(None) is None

    def test_the_raise_is_the_probe_up_minus_the_settle(self):
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": 12, "back_down_mm": 5}}) == 7.0
        assert raise_clearance_mm(None) is None
        assert raise_clearance_mm({"raise_before_travel": {"probe_up_mm": "up"}}) is None
        assert raise_clearance_mm({"chute": {}}) is None


def _lean_start(bambu, monkeypatch):
    """Let the start_print template reach its success block without a printer."""
    import kiln.printers.base as base
    from kiln.printers.print_gate import run_adapter_gate  # noqa: F401 -- import path pinned

    bambu._printer_model = "bambu_a1"
    monkeypatch.setattr("kiln.printers.print_gate.run_adapter_gate", lambda *a, **k: None)
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", ())
    monkeypatch.setattr(bambu, "_start_print_impl", lambda file_name, **kw: PrintResult(success=True, message="ok"))


class TestStartPrintMarksOccupied:
    """The engine: every door that starts a print runs the start_print template."""

    def test_a_started_print_records_the_file_and_its_height(self, bambu, monkeypatch, tmp_path):
        from kiln.monitor_twin import note_sliced, note_wrapped

        _lean_start(bambu, monkeypatch)
        tag = uuid.uuid4().hex[:8]
        out = tmp_path / f"coaster-{tag}.gcode"
        out.write_bytes(b";Z:0.2\n;Z:18.4\n")
        note_sliced(str(tmp_path / f"coaster-{tag}.stl"), str(out))
        note_wrapped(str(out), str(tmp_path / f"coaster-{tag}.gcode.3mf"))
        assert bambu.start_print(f"coaster-{tag}.gcode.3mf", plate_number=1).success
        state = plate_occupancy(bambu)
        assert state.occupied and state.source == "kiln_started_print"
        assert state.job.file == f"coaster-{tag}.gcode.3mf" and state.job.max_z_mm == 18.4
        assert state.job.printer_id == "bambu_a1"

    def test_a_file_without_geometry_is_still_occupied(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        assert bambu.start_print(f"mystery-{uuid.uuid4().hex[:6]}.3mf").success
        state = plate_occupancy(bambu)
        assert state.occupied and state.job.max_z_mm is None and state.job.footprint_mm is None

    def test_a_cleared_plate_becomes_occupied_again_on_the_next_start(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        mark_clear(bambu, "human")
        assert plate_occupancy(bambu).clear
        bambu.start_print("next.3mf")
        assert plate_occupancy(bambu).occupied

    def test_a_refused_start_records_nothing(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        monkeypatch.setattr(bambu, "_start_print_impl", lambda file_name, **kw: PrintResult(success=False, message="no"))
        bambu.start_print("nope.3mf")
        assert plate_occupancy(bambu).status == "unknown"

    def test_the_record_never_blocks_a_print(self, bambu, monkeypatch):
        _lean_start(bambu, monkeypatch)
        monkeypatch.setattr("kiln.plate_state.mark_occupied_by_start", mock.Mock(side_effect=RuntimeError("disk")))
        assert bambu.start_print("fine.3mf").success


class TestPrintEndedKeepsOccupied:
    def _server(self, monkeypatch, bambu):
        import kiln.server as srv

        registry = mock.Mock()
        registry.get = lambda name: bambu if name == "default" else (_ for _ in ()).throw(KeyError(name))
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_stop_print_watchdog", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_is_heater_watchdog_machine", lambda adapter: False)
        return srv

    def test_the_event_reasserts_occupied_and_keeps_the_job(self, bambu, monkeypatch):
        srv = self._server(monkeypatch, bambu)
        mark_occupied(bambu, PlateJob(file="coaster.3mf", max_z_mm=18.0))
        srv._on_print_ended_event(SimpleNamespace(data={"printer_name": "default"}, source="recovery:default"))
        state = plate_occupancy(bambu)
        assert state.occupied and state.source == "print_ended" and state.job.file == "coaster.3mf"
        assert state.job.max_z_mm == 18.0

    def test_an_ending_never_marks_the_plate_clear(self, bambu, monkeypatch):
        srv = self._server(monkeypatch, bambu)
        mark_clear(bambu, "human")
        srv._on_print_ended_event(SimpleNamespace(data={"printer_name": "default"}, source=""))
        assert plate_occupancy(bambu).occupied

    def test_the_status_edge_hook_is_the_same_moment(self, bambu, monkeypatch):
        srv = self._server(monkeypatch, bambu)
        mark_occupied(bambu, PlateJob(file="coaster.3mf", max_z_mm=18.0))
        srv._note_plate_after_print_ended_by_name("default")
        state = plate_occupancy(bambu)
        assert state.source == "print_ended" and state.job.file == "coaster.3mf"

    def test_an_unknown_or_empty_name_records_nothing(self, bambu, monkeypatch):
        srv = self._server(monkeypatch, bambu)
        srv._note_plate_after_print_ended_by_name("")
        srv._note_plate_after_print_ended_by_name("someone-else")
        assert plate_occupancy(bambu).status == "unknown"

    def test_the_hook_is_registered_beside_the_watchdog_retire(self):
        import kiln.printers.base as base
        import kiln.server as srv

        srv._install_print_lifecycle_hooks()
        assert srv._note_plate_after_print_ended_by_name in base._PRINT_ENDED_HOOKS


class TestDoors:
    @pytest.fixture
    def door(self, monkeypatch, bambu):
        import kiln.server as srv

        bambu._printer_model = "bambu_a1"
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, name or "default"))
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        return bambu

    def test_plate_status_tool_reads_the_record_without_a_round_trip(self, door):
        from kiln.plugins.homing_tools import plate_status

        mark_occupied(door, PlateJob(file="coaster.3mf", max_z_mm=18.0))
        monkeypatch_calls = mock.Mock()
        door.get_state = monkeypatch_calls
        out = plate_status()
        assert out["success"] is True and out["printer_name"] == "default"
        assert out["plate"]["status"] == "occupied" and out["plate"]["job"]["max_z_mm"] == 18.0
        assert "coaster.3mf" in out["plate"]["description"]
        monkeypatch_calls.assert_not_called()

    def test_plate_status_is_read_only_and_there_is_no_clear_tool(self):
        from kiln.plugins.homing_tools import plugin

        mcp = mock.Mock()
        plugin.register(mcp)
        names = {call.args[0].__name__ for call in mcp.tool.return_value.call_args_list}
        assert "plate_status" in names and not any("clear" in n for n in names)
        import inspect

        assert "plate_clear" not in inspect.signature(__import__("kiln.plugins.homing_tools", fromlist=["plate_status"]).plate_status).parameters

    def test_kiln_plate_status_and_clear(self, door):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        mark_occupied(door, PlateJob(file="coaster.3mf", max_z_mm=18.0))
        result = CliRunner().invoke(cli, ["plate", "status", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["data"]["plate"]["status"] == "occupied"

        result = CliRunner().invoke(cli, ["plate", "clear", "--note", "took the coaster off", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["data"]["plate"]["status"] == "clear" and payload["data"]["plate"]["source"] == "human"
        assert plate_occupancy(door).note == "took the coaster off"

    def test_kiln_plate_clear_fails_loudly_for_an_unidentifiable_machine(self, monkeypatch):
        from click.testing import CliRunner

        import kiln.server as srv
        from kiln.cli.main import cli

        adapter = mock.MagicMock(spec=PrinterAdapter)
        adapter.name = "octo"
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (adapter, "default"))
        result = CliRunner().invoke(cli, ["plate", "clear", "--json"])
        assert result.exit_code == 1
        assert "neither a serial nor an address" in json.dumps(json.loads(result.output[result.output.index("{"):]))

    def test_the_cli_doors_call_the_shared_runtime_config(self):
        import inspect

        from kiln.cli import main as cli_main

        for cmd in (cli_main.plate_status_cmd, cli_main.plate_clear_cmd):
            assert "ensure_runtime_config()" in inspect.getsource(cmd.callback)


class _DoctorAdapter:
    name = "bambu"
    serial = "01P00A000000001"
    _printer_model = "bambu_a1"

    def __init__(self):
        self.capabilities = SimpleNamespace(can_send_gcode=True, can_snapshot=False)

    def get_state(self):
        from kiln.printers.base import PrinterState, PrinterStatus

        return PrinterState(connected=True, state=PrinterStatus.IDLE)

    def get_identity_channels(self):
        return {}

    def get_printer_info(self):
        return None

    def purge_station(self):
        return {"printer_id": "bambu_a1", "raise_before_travel": {"probe_up_mm": 40, "back_down_mm": 15}}


class TestDoctorLine:
    def _plate_check(self, monkeypatch, adapter):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.main.load_printer_config",
                            lambda *a, **k: {"name": "default", "type": "bambu", "host": "192.168.1.6"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda *a, **k: adapter)
        output = CliRunner().invoke(cli, ["doctor", "--json"]).output
        data = json.loads(output[output.index("{"):])
        return next((c for c in data["checks"] if c["name"] == "plate"), None)

    def test_unknown_says_kiln_has_no_record(self, monkeypatch):
        check = self._plate_check(monkeypatch, _DoctorAdapter())
        assert check is not None and check["ok"] is True and check["warn"] is False
        assert check["detail"].startswith("unknown") and "kiln plate clear" in check["detail"]

    def test_a_tall_part_warns_and_names_the_raise(self, monkeypatch):
        adapter = _DoctorAdapter()
        mark_occupied(adapter, PlateJob(file="vase.gcode.3mf", max_z_mm=60.0))
        check = self._plate_check(monkeypatch, adapter)
        assert check["warn"] is True and "vase.gcode.3mf" in check["detail"]
        assert "taller than the 25 mm raise" in check["detail"] and "refuse" in check["detail"]

    def test_clear_says_who_and_that_it_stays(self, monkeypatch):
        adapter = _DoctorAdapter()
        mark_clear(adapter, "human")
        check = self._plate_check(monkeypatch, adapter)
        assert check["warn"] is False and check["detail"].startswith("clear") and "next print" in check["detail"]
