"""The folder watcher never starts a dropped file onto the last print.

``kiln ingest watch --auto-queue`` starts each file dropped in a folder on an
idle printer, with nobody there.  It checked that the printer was idle and
nothing else, so the second file started its maker start sequence across the
first file's finished part.  The queue learned to wait for the plate on
2026-10-26; the watcher had been named "out of scope" beside it.  It now asks
the same plate record before every start, keeps the file in place, and says
the wait once.

A/B: with the ``start_hold`` check in ``_dispatch_pending`` removed, the
first test fails at ``a.upload_file.assert_not_called()``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, PropertyMock

import pytest
from click.testing import CliRunner

from kiln.plate_state import PlateJob, mark_clear, mark_occupied
from kiln.printers.base import JobProgress, PrinterCapabilities, PrinterState, PrinterStatus, PrintResult, UploadResult

PART = PlateJob(file="vase_v3.gcode", max_z_mm=42.0)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as emergency

    monkeypatch.setattr(emergency, "_coordinator", None)


@pytest.fixture()
def watcher(tmp_path, monkeypatch):
    import kiln.cli.main as cli_main
    import kiln.cli.print_gate as cli_gate

    a = MagicMock()
    type(a).name = PropertyMock(return_value="A")
    type(a).capabilities = PropertyMock(return_value=PrinterCapabilities())
    a.serial = "SER-A"
    a.snapshot_source = None
    a.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
    a.get_job.return_value = JobProgress()
    a.upload_file.return_value = UploadResult(success=True, file_name="lid.gcode", message="ok")
    a.start_print.return_value = PrintResult(success=True, message="started")

    monkeypatch.setattr(cli_main, "_load_fleet_adapters", lambda printer_filter=None: ({"A": a}, []))
    monkeypatch.setattr(cli_main, "_route_printer_for_job", lambda **kw: ("A", None, None))
    monkeypatch.setattr(cli_gate, "confirm_standing_auto_print_at_terminal", lambda **kw: None)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "lid.gcode").write_text("G28\nG1 X10 Y10\n")

    def run() -> dict:
        result = CliRunner().invoke(
            cli_main.cli,
            ["ingest", "watch", "--dir", str(drop), "--auto-queue", "--once", "--printer", "A", "--json"],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output[result.output.index("{"):])["data"]

    return a, run


def test_a_part_on_the_plate_holds_the_dropped_file(watcher):
    a, run = watcher
    mark_occupied(a, PART)
    out = run()
    a.upload_file.assert_not_called()
    a.start_print.assert_not_called()
    assert out["dispatched"] == []
    assert [w["printer"] for w in out["waiting"]] == ["A"]
    assert out["waiting"][0]["code"] == "PLATE_OCCUPIED_START_NOT_YET"
    assert out["waiting"][0]["sentence"].startswith("lid is next on A. The last print, vase v3, is still on the plate")
    assert out["pending"]["A"], "the file keeps its place"


def test_a_clear_plate_starts_it(watcher):
    a, run = watcher
    mark_occupied(a, PART)
    run()
    mark_clear(a, "human")
    out = run()
    assert a.start_print.call_count == 1
    assert [d["printer"] for d in out["dispatched"]] == ["A"]
    assert out["waiting"] == []
