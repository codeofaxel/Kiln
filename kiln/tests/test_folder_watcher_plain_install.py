"""The folder watcher on a computer without the fleet router (a plain install).

``kiln ingest watch --auto-queue`` asked the router which printer takes each
dropped file, and imported it with nothing around it.  The router is
kiln-pro's, so on every plain install the watcher crashed on the first file,
even with ONE printer named, where there is nothing to choose.  Every other
watcher test replaces the routing step, which is how it stayed hidden.

Now one eligible printer takes the file with no routing at all, and several
(Kiln Business) are routed by Kiln's servers, the way ``route_print_job`` is;
a refusal or a miss routes nothing and says why.  These tests run the real
routing step with the router unimportable.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, PropertyMock

import pytest
from click.testing import CliRunner

from kiln.printers.base import JobProgress, PrinterCapabilities, PrinterState, PrinterStatus, PrintResult, UploadResult


@pytest.fixture(autouse=True)
def _plain_install(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as emergency

    monkeypatch.setattr(emergency, "_coordinator", None)
    # The router is kiln-pro's: on a plain install the name does not import.
    monkeypatch.setitem(sys.modules, "kiln.job_router", None)


def _printer(name: str) -> MagicMock:
    p = MagicMock()
    type(p).name = PropertyMock(return_value=name)
    type(p).capabilities = PropertyMock(return_value=PrinterCapabilities())
    p.serial = f"SER-{name}"
    p.snapshot_source = None
    p.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
    p.get_job.return_value = JobProgress()
    p.upload_file.return_value = UploadResult(success=True, file_name="lid.gcode", message="ok")
    p.start_print.return_value = PrintResult(success=True, message="started")
    return p


@pytest.fixture()
def watch(tmp_path, monkeypatch):
    import kiln.cli.main as cli_main
    import kiln.cli.print_gate as cli_gate

    printers: dict[str, MagicMock] = {}

    def _load(printer_filter=None):
        if printer_filter:
            return {printer_filter: printers[printer_filter]}, []
        return dict(printers), []

    monkeypatch.setattr(cli_main, "_load_fleet_adapters", _load)
    monkeypatch.setattr(cli_gate, "confirm_standing_auto_print_at_terminal", lambda **kw: None)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "lid.gcode").write_text("G28\nG1 X10 Y10\n")

    def run(*extra: str) -> dict:
        result = CliRunner().invoke(
            cli_main.cli, ["ingest", "watch", "--dir", str(drop), "--auto-queue", "--once", "--json", *extra],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output[result.output.index("{"):])["data"]

    return printers, run


def test_one_named_printer_takes_the_file_without_a_router(watch, monkeypatch):
    printers, run = watch
    printers["A"] = _printer("A")
    asked = []
    monkeypatch.setattr("kiln.plugins.fleet_tools._served_route", lambda *a, **kw: asked.append(a) or {})
    out = run("--printer", "A")
    assert [q["printer"] for q in out["queued"]] == ["A"]
    assert [d["printer"] for d in out["dispatched"]] == ["A"]
    printers["A"].start_print.assert_called_once()
    # One printer is not a routing question: nothing was asked of the servers.
    assert asked == []


@pytest.fixture()
def business(monkeypatch):
    import kiln.printers.engagement as engagement

    monkeypatch.setattr(engagement, "multi_machine_tier", lambda *a, **k: True)


def test_several_printers_are_routed_by_kilns_servers(watch, business, monkeypatch):
    printers, run = watch
    printers["A"] = _printer("A")
    printers["B"] = _printer("B")
    asked = []

    def _servers(tool, **kw):
        asked.append((tool, sorted(c["printer_id"] for c in kw["candidates"])))
        return {"success": True, "routing": {"recommended_printer": {"printer_id": "B"}, "why": "B is free first."}}

    monkeypatch.setattr("kiln.server._pro_api_call", _servers)
    out = run()
    assert asked == [("plan_fleet_route", ["A", "B"])]
    assert out["queued"] == [{"file": out["queued"][0]["file"], "printer": "B", "why": "B is free first."}]
    printers["B"].start_print.assert_called_once()
    printers["A"].start_print.assert_not_called()


def test_a_route_the_servers_refuse_routes_nothing_and_says_why(watch, business, monkeypatch):
    printers, run = watch
    printers["A"] = _printer("A")
    printers["B"] = _printer("B")
    refusal = {"success": False, "error": {"code": "NO_ROOM", "message": "No plate has room for this part. Nothing was routed."}}
    monkeypatch.setattr("kiln.server._pro_api_call", lambda tool, **kw: refusal)
    out = run()
    assert out["queued"] == [] and out["dispatched"] == []
    assert any("No plate has room for this part" in e for e in out["errors"])


def test_offline_routes_nothing_and_says_so(watch, business, monkeypatch):
    import errno

    printers, run = watch
    printers["A"] = _printer("A")
    printers["B"] = _printer("B")

    def _offline(tool, **kw):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr("kiln.server._pro_api_call", _offline)
    out = run()
    assert out["queued"] == [] and out["dispatched"] == []
    assert any("this computer is offline" in e and "won't route it anywhere" in e for e in out["errors"])
