"""What Kiln says when a printer it slices for more than one nozzle has no
maker's slicer here to do it.

Covered here, with an invented two-nozzle machine served as its record: the
setup line is said once per printer, and not at all when the slicer is ready,
the printer is sliced for one nozzle, no record is served, or on Kiln's shared
server; the owner's fitted nozzles count; the checkup's row is said every run;
where to get the slicer names the maker's page and, on a Mac with Homebrew,
the command; and every door that adds a printer, sets its model or records a
fitted nozzle carries the line.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from kiln import maker_slicer, maker_slicer_notes
from kiln.maker_slicer import Miss, install_words
from kiln.slicer import SlicerInfo

_TWO = {"kind": "fixed_pair", "stock": 2, "max": 2, "sliced_as": 2, "maker_slicer": {"name": "bambu_studio"}}
#: An invented machine that ships with one nozzle and takes three.
_ADD_ON = {
    "kind": "toolchanger", "stock": 1, "max": 3, "sliced_as": 1,
    "positions": [{"id": i, "reach": "0x0,300x0,300x300,0x300"} for i in (1, 2, 3)],
    "maker_slicer": {"name": "prusaslicer", "min_version": "2.5.0"},
}
_RECORDS = {
    "invented_two": {"display_name": "Invented Two", "nozzles": _TWO},
    "invented_add_on": {"display_name": "Invented Add-On", "nozzles": _ADD_ON},
    "invented_one": {"display_name": "Invented One"},
}
_LINE = (
    "Your Invented Two has two nozzles. Kiln uses both once Bambu Studio is installed (free). "
    "Until then, multi-colour prints use one nozzle with filament swaps. Get it from "
    "bambulab.com/download/studio."
)


def _served(spelling: str, block: str):
    assert block == "slicer"
    return (spelling, dict(_RECORDS[spelling])) if spelling in _RECORDS else None


@pytest.fixture
def served(tmp_path, monkeypatch):
    """The invented records served, nothing installed, what was said kept in the test's own file."""
    monkeypatch.setattr("kiln.printer_intelligence.catalogue_block", _served)
    monkeypatch.setattr(maker_slicer_notes, "store_path", lambda: tmp_path / "notes.json")
    monkeypatch.setattr(maker_slicer_notes, "_hosted", lambda: False)
    monkeypatch.setattr(maker_slicer, "_brew_command", lambda *_a, **_k: None)
    monkeypatch.setattr(maker_slicer, "_find", lambda _slicer_id: None)
    return tmp_path / "notes.json"


# ---------------------------------------------------------------------------
# The setup line
# ---------------------------------------------------------------------------


class TestTheSetupLine:
    def test_it_is_said_once_per_printer(self, served):
        assert maker_slicer_notes.setup_line("invented_two") == _LINE
        assert maker_slicer_notes.setup_line("invented_two") == ""
        assert json.loads(served.read_text())["invented_two"]["suggested"]

    def test_nothing_is_said_once_the_slicer_is_ready(self, served, monkeypatch):
        monkeypatch.setattr(maker_slicer, "_find", lambda _id: SlicerInfo(path="/fake/BambuStudio", name="bambustudio"))
        assert maker_slicer_notes.setup_line("invented_two") == ""
        assert not served.exists()

    def test_a_slicer_too_old_is_to_be_updated(self, served, monkeypatch):
        old = SlicerInfo(path="/fake/PrusaSlicer", name="prusa-slicer", version="PrusaSlicer-2.4.1 based on Slic3r")
        monkeypatch.setattr(maker_slicer, "_find", lambda _id: old)
        fitted = [{"id": i, "fitted": True} for i in (1, 2, 3)]
        line = maker_slicer_notes.setup_line("invented_add_on", positions=fitted)
        assert line.startswith("Your Invented Add-On has 3 nozzles. Kiln uses all 3 once PrusaSlicer is updated to 2.5.0")
        assert line.endswith("Get it from prusa3d.com/p/prusaslicer.")

    def test_a_printer_sliced_for_one_counts_the_nozzles_its_owner_fitted(self, served):
        assert maker_slicer_notes.setup_line("invented_add_on") == ""
        assert maker_slicer_notes.setup_line("invented_add_on", positions=[{"id": 1, "fitted": True}]) == ""
        two = [{"id": 1, "fitted": True}, {"id": 2, "fitted": True}]
        assert "Invented Add-On has two nozzles" in maker_slicer_notes.setup_line("invented_add_on", positions=two)

    @pytest.mark.parametrize("model", ["invented_one", "not_served", None, ""])
    def test_no_line_without_a_record_for_more_than_one_nozzle(self, served, model):
        assert maker_slicer_notes.setup_line(model) == ""
        assert not served.exists()

    def test_the_shared_server_says_nothing_and_keeps_nothing(self, served, monkeypatch):
        monkeypatch.setattr(maker_slicer_notes, "_hosted", lambda: True)
        assert maker_slicer_notes.setup_line("invented_two") == ""
        assert not served.exists()

    def test_a_nozzle_record_with_two_fitted_carries_the_line(self, served, monkeypatch):
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: "invented_add_on")
        fitted = {"success": True, "positions": [{"id": 1, "fitted": True}, {"id": 2, "fitted": True}]}
        assert "Invented Add-On has two nozzles" in maker_slicer_notes.after_nozzle_record(fitted, "garage")["two_nozzles"]
        one = {"success": True, "positions": [{"id": 1, "fitted": True}, {"id": 2, "fitted": False}]}
        refused = {"success": False, "positions": fitted["positions"]}
        assert "two_nozzles" not in maker_slicer_notes.after_nozzle_record(one, "garage")
        assert "two_nozzles" not in maker_slicer_notes.after_nozzle_record(refused, "garage")


# ---------------------------------------------------------------------------
# The checkup's row
# ---------------------------------------------------------------------------


class TestTheCheckupRow:
    def test_a_missing_slicer_is_named_every_run(self, served):
        row = {"name": "two_nozzles", "ok": True, "warn": True,
               "detail": "install Bambu Studio to use both: bambulab.com/download/studio"}
        assert maker_slicer_notes.doctor_row("invented_two") == row
        assert maker_slicer_notes.doctor_row("invented_two") == row
        assert not served.exists()

    def test_a_ready_slicer_reads_ready(self, served, monkeypatch):
        monkeypatch.setattr(maker_slicer, "_find", lambda _id: SlicerInfo(path="/fake/BambuStudio", name="bambustudio"))
        assert maker_slicer_notes.doctor_row("invented_two") == {"name": "two_nozzles", "ok": True, "detail": "ready"}

    def test_the_command_rides_the_row(self, served, monkeypatch):
        monkeypatch.setattr(maker_slicer, "_brew_command", lambda cask, update: f"brew install --cask {cask}")
        row = maker_slicer_notes.doctor_row("invented_two")
        assert row["command"] == "brew install --cask bambu-studio"
        assert row["detail"].endswith(", or on this Mac: brew install --cask bambu-studio")

    def test_no_row_for_a_printer_sliced_for_one_nozzle(self, served):
        assert maker_slicer_notes.doctor_row("invented_one") is None
        assert maker_slicer_notes.doctor_row("invented_add_on") is None


# ---------------------------------------------------------------------------
# Where to get it
# ---------------------------------------------------------------------------


class TestWhereToGetIt:
    def _mac(self, monkeypatch, tmp_path, *, brew: bool = True) -> Path:
        prefix = tmp_path / "homebrew"
        (prefix / "bin").mkdir(parents=True)
        monkeypatch.setattr(maker_slicer.sys, "platform", "darwin")
        monkeypatch.setattr(maker_slicer.shutil, "which", lambda name: str(prefix / "bin" / "brew") if brew else None)
        return prefix

    def test_every_computer_is_given_the_makers_page(self, monkeypatch):
        monkeypatch.setattr(maker_slicer.sys, "platform", "linux")
        assert install_words(Miss("bambu_studio", "install")) == {"download": "bambulab.com/download/studio"}
        assert install_words(Miss("prusaslicer", "update")) == {"download": "prusa3d.com/p/prusaslicer"}

    def test_a_mac_with_homebrew_is_given_the_install_command(self, monkeypatch, tmp_path):
        self._mac(monkeypatch, tmp_path)
        assert install_words(Miss("bambu_studio", "install"))["command"] == "brew install --cask bambu-studio"
        assert install_words(Miss("prusaslicer", "install"))["command"] == "brew install --cask prusaslicer"

    def test_an_update_command_only_for_a_copy_homebrew_installed(self, monkeypatch, tmp_path):
        prefix = self._mac(monkeypatch, tmp_path)
        assert "command" not in install_words(Miss("prusaslicer", "update"))
        (prefix / "Caskroom" / "prusaslicer").mkdir(parents=True)
        assert install_words(Miss("prusaslicer", "update"))["command"] == "brew upgrade --cask prusaslicer"

    def test_no_command_without_homebrew(self, monkeypatch, tmp_path):
        self._mac(monkeypatch, tmp_path, brew=False)
        assert "command" not in install_words(Miss("bambu_studio", "install"))


# ---------------------------------------------------------------------------
# Every door that adds a printer, sets its model or records a fitted nozzle
# ---------------------------------------------------------------------------


@pytest.fixture
def line(monkeypatch):
    """The setup line stubbed, recording the model each door asked about."""
    asked: list[tuple[object, object]] = []

    def setup_line(model, *, positions=None):
        asked.append((model, positions))
        return "NOZZLES LINE"

    monkeypatch.setattr(maker_slicer_notes, "setup_line", setup_line)
    return asked


class TestEveryDoorCarriesTheLine:
    def test_register_printer(self, line, monkeypatch, tmp_path):
        import requests

        from kiln import bench, server

        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        monkeypatch.setattr(bench, "offer_after_registration", lambda _adapter, name: None)
        monkeypatch.setattr(requests, "head", lambda *_a, **_k: object())
        monkeypatch.setattr(server, "read_machine_identity_bounded", lambda _read: None)
        monkeypatch.setattr("kiln.camera_check.camera_setup_line", lambda *_a, **_k: (None, ""))
        reply = server.register_printer(
            name="garage-printer", printer_type="moonraker", host="192.0.2.21",
            printer_model="neptune4_plus", printer_make="Elegoo", persist=False, verify_connection=False,
        )
        assert reply["success"] is True, reply
        assert reply["two_nozzles"] == "NOZZLES LINE" and reply["message"].endswith(" NOZZLES LINE")
        assert line == [("elegoo_neptune4_plus", None)]

    def test_set_printer_model(self, line, tmp_path):
        from kiln.cli.config import save_printer
        from kiln.printer_setup import set_printer_model

        config = tmp_path / "config.yaml"
        save_printer("bench", "moonraker", "192.0.2.5", config_path=config, probe=False)
        result = set_printer_model("ender3", printer_name="bench", config_path=config)
        assert result["applied"] is True, result
        assert result["two_nozzles"] == "NOZZLES LINE" and result["message"].endswith("NOZZLES LINE")
        assert line == [("ender3", None)]

    def test_kiln_auth(self, line, tmp_path):
        from kiln.cli.config import PrinterSaveResult
        from kiln.cli.main import cli

        with patch(
            "kiln.cli.main.save_printer", return_value=PrinterSaveResult(tmp_path / "config.yaml", "bench", "added"),
        ):
            args = ["auth", "--name", "bench", "--host", "/dev/ttyUSB0", "--type", "usb", "--printer-model", "ender3"]
            text = CliRunner().invoke(cli, args)
            data = json.loads(CliRunner().invoke(cli, [*args, "--json"]).output)["data"]
        assert text.exit_code == 0, text.output
        assert "NOZZLES LINE" in text.output.splitlines()
        assert data["two_nozzles"] == "NOZZLES LINE"
        assert line == [("ender3", None), ("ender3", None)]

    def test_kiln_setup(self, line, monkeypatch, tmp_path):
        from kiln.cli.main import cli
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        found = DiscoveredPrinter(host="192.168.1.9", port=7125, printer_type="moonraker", name="K1")
        printer = SimpleNamespace(get_state=lambda: SimpleNamespace(connected=True, state=SimpleNamespace(value="idle")))
        with (
            patch("kiln.cli.discovery.discover_printers", return_value=[found]),
            patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value="k1c"),
            patch("kiln.cli.main._make_adapter", return_value=printer),
            patch("kiln.camera_check.camera_setup_line", lambda *_a, **_k: (None, "")),
        ):
            result = CliRunner().invoke(cli, ["setup"], input="1\nk1\n\n" + "\n" * 8)
        assert "  NOZZLES LINE" in result.output.splitlines(), result.output
        assert line == [("k1c", None)]

    def test_the_bridge_first_printer(self, line, monkeypatch, capsys):
        import click

        import kiln.cli.bridge_commands as bcmd
        import kiln.cli.config as cfg
        import kiln.cli.discovery as disc
        import kiln.cli.printer_model_prompt as pmp
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr(cfg, "list_printers", lambda **_kw: [])
        monkeypatch.setattr(cfg, "save_printer", lambda *_a, **_kw: "/tmp/config.yaml")
        monkeypatch.setattr(cfg, "load_printer_config", lambda _name: {"type": "moonraker", "host": "x"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: object())
        monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: True)
        found = DiscoveredPrinter(host="192.168.1.23", port=7125, printer_type="moonraker", name="K1C")
        monkeypatch.setattr(disc, "discover_printers", lambda *_a, **_kw: [found])
        monkeypatch.setattr(pmp, "prompt_for_printer_model", lambda *_a, **_kw: "k1c")
        monkeypatch.setattr(click, "confirm", lambda *_a, **_kw: True)
        monkeypatch.setattr("kiln.camera_check.camera_setup_line", lambda *_a, **_k: (None, ""))

        bcmd._offer_first_printer()

        assert "  NOZZLES LINE" in capsys.readouterr().out.splitlines()
        assert line == [("k1c", None)]

    def test_the_served_nozzle_record(self, line, monkeypatch):
        import kiln.server as srv

        class _MCP:
            def __init__(self):
                self.tools = {}

            def tool(self, **_kw):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return deco

        positions = [{"id": 1, "fitted": True}, {"id": 2, "fitted": True}]
        monkeypatch.setattr(srv, "_pro_api_call", lambda name, **kw: {"success": True, "positions": positions})
        monkeypatch.setattr("kiln.printer_nozzle_reading.with_local_reading", lambda name, kw: kw)
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: "invented_add_on")
        fake = _MCP()
        srv._register_pro_tool_stubs(fake)
        answer = fake.tools["set_nozzle_state"](printer_id="garage", fitted_positions=[1, 2])
        assert answer["two_nozzles"] == "NOZZLES LINE"
        assert line == [("invented_add_on", [
            {"id": 1, "fitted": True, "diameter_mm": None, "material": None},
            {"id": 2, "fitted": True, "diameter_mm": None, "material": None},
        ])]

    def test_the_checkup(self, monkeypatch, tmp_path):
        from kiln.cli.main import cli

        rows: list[tuple[object, object]] = []

        def doctor_row(model, *, positions=None):
            rows.append((model, positions))
            return {"name": "two_nozzles", "ok": True, "warn": True, "detail": "install Bambu Studio to use both"}

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(maker_slicer_notes, "doctor_row", doctor_row)
        monkeypatch.setattr(
            "kiln.cli.main.load_printer_config",
            lambda *_a, **_k: {"name": "garage", "type": "bambu", "host": "192.0.2.60", "printer_model": "invented_two"},
        )
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: (_ for _ in ()).throw(OSError("offline")))
        monkeypatch.setattr("kiln.print_doors.summarize", lambda: (True, "not walked here"))
        monkeypatch.setattr(
            "kiln._pro_nozzle_bridge.consult_recorded_nozzle", lambda name: {"answered": True, "positions": None},
        )
        result = CliRunner().invoke(cli, ["doctor"])
        assert "  ⚠ Two Nozzles: install Bambu Studio to use both" in result.output.splitlines(), result.output
        assert rows == [("invented_two", None)]
