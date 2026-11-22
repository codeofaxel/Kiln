"""A move Kiln plans by model is refused while the printer names another model.

Kiln sends its own head moves for a home, a park, a wipe, a purge and the
park before a filament change, and plans them for the model the printer was
set up as.  A Bambu Lab printer says what it is in its serial number and its
firmware name, so every one of those doors refuses when that answer names a
different catalogue printer, and says the one command that settles it.

The refusal needs proof.  A serial number Kiln has no model for, a printer
with no model set, a model outside the catalogue, and two answers from the
printer that disagree with each other all let the move run.  Stopping,
pausing, cancelling and reading the printer are never asked.  Setting the
model is refused the same way.

The printer's owner can settle it the other way, for that one machine: they
say it is the model it was set up as, or that an upgrade its maker sells is
fitted.  Only a person says it (a code shown on this computer's screen, or a
person at a terminal); it is kept on the printer's own saved record, and
setting the model to what the printer reports takes it back.

Every door test here drives the real adapter template or the registered
tool and asserts on what reached the printer.
"""

from __future__ import annotations

import pytest
import yaml

from kiln.printers.base import PrinterAdapter, PrinterError, PrinterInfo, PrinterState, PrinterStatus
from kiln.printers.command_verdict import CommandVerdict

from .test_filament_handling import (  # noqa: F401
    _build,
    _published,
    _scripts,
    bambu,
)
from .test_home_axes import _fake_home_doc, _idle, _serve

# ruff: noqa: F811  -- `bambu` is a fixture, re-used by name

CODE = "PRINTER_MODEL_DISAGREES"

#: A serial number of each model, by the prefix the printer's maker gives it.
A1 = "03900A000000001"
X2D = "20P00A000000001"
P1P = "01S00A000000001"
P1S = "01P00A000000001"
UNLISTED = "ZZZ00A000000001"
KIT = "bambu_p1p_enclosure_kit"

#: Every door that sends a move Kiln planned, as the adapter's own template.
MOVE_DOORS = {
    "home": lambda a: a.home_axes(),
    "home, one step": lambda a: a.home_axes(step=1),
    "home, plan only": lambda a: a.home_axes(plan_only=True),
    "park": lambda a: a.park_head(),
    "wipe": lambda a: a.wipe_nozzle(material="PLA"),
    "wipe, one step": lambda a: a.wipe_nozzle(material="PLA", step=1),
    "purge": lambda a: a.purge_filament(material="PLA"),
    "load": lambda a: a.load_filament(slot=0, material="PLA"),
    "unload": lambda a: a.unload_filament(material="PLA"),
}


@pytest.fixture(autouse=True)
def saved(tmp_path, monkeypatch):
    """Each test has its own plate record, tally file and saved printers (none, so no owner's word)."""
    from kiln import daily_stats

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"printers": {}}), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: path)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", path)
    monkeypatch.setattr("kiln.printer_model_resolver._cache", (0.0, None))
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    return path


def _owner_said(path, *, serial, model, upgrade=None, name="garage", statement_serial=None):
    """A saved printer whose record carries its owner's statement, as the doors write it."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    said = {"serial": statement_serial or serial, "model": model, "how": "user_terminal",
            "by": "os_user:test", "said_at": "2026-11-19T10:00:00+00:00"}
    if upgrade:
        said["upgrade"] = upgrade
    raw.setdefault("printers", {})[name] = {
        "type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": serial,
        "printer_model": model, "owner_says": said,
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


def _an_x2d_set_up_as_an_a1(bambu, monkeypatch, *, plans=None):
    """The printer answers as an X2D; its settings say A1; an A1 plan is on offer."""
    bambu._serial = X2D
    bambu._printer_model = "bambu_a1"
    _idle(bambu, monkeypatch)
    docs = plans if plans is not None else {
        "home": _fake_home_doc(), "park": _fake_home_doc(verb="park"),
        "purge": {**_fake_home_doc(verb="park"), "verb": "purge", "pre_gcode": ["G1 X-9 F100"], "park_gcode": ["G1 X-9 F100"]},
    }
    return _serve(monkeypatch, docs)


class TestEveryMoveDoorRefuses:
    @pytest.mark.parametrize("door", sorted(MOVE_DOORS))
    def test_nothing_is_sent_and_no_plan_is_asked_for(self, bambu, monkeypatch, door):
        asks = _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        with pytest.raises(PrinterError) as refused:
            MOVE_DOORS[door](bambu)
        assert _published(bambu) == [], "a command reached a printer set up as another model"
        assert asks == [], "a plan for the wrong model was asked for"
        said = str(refused.value)
        assert "set up as the Bambu Lab A1" in said and "reports itself as the Bambu Lab X2D" in said

    def test_the_refusal_names_the_command_that_settles_it(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._kiln_registered_name = "garage"
        with pytest.raises(PrinterError) as refused:
            bambu.home_axes()
        said = str(refused.value)
        assert "Nothing was sent to garage." in said
        assert "kiln set-model bambu_x2d --printer garage --replace" in said
        assert "the printer's own screen" in said

    def test_the_owners_way_out_is_named_and_it_takes_a_person(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._kiln_registered_name = "garage"
        with pytest.raises(PrinterError) as refused:
            bambu.park_head()
        said = str(refused.value)
        assert "If it really is the Bambu Lab A1, its owner can say so with" in said
        assert "kiln set-model bambu_a1 --printer garage --owner-says-so` at a terminal" in said
        assert "owner_says_so=true and the code Kiln then shows on this computer's screen" in said
        assert "KILN_" not in said and bambu._serial not in said

    def test_a_firmware_name_alone_is_proof(self, bambu, monkeypatch):
        """A serial number Kiln has no model for, and firmware that names the X2D."""
        asks = _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial = UNLISTED
        bambu._fw_modules = [{"name": "ota", "product_name": "Bambu Lab X2D"}]
        with pytest.raises(PrinterError, match="reports itself as the Bambu Lab X2D"):
            bambu.home_axes()
        assert _published(bambu) == [] and asks == []

    def test_the_refusal_is_tallied_by_model_code_and_cause(self, bambu, monkeypatch):
        from kiln import daily_stats

        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        with pytest.raises(PrinterError):
            bambu.home_axes()
        assert daily_stats.get_daily_stats()["motion_refusals"] == {f"bambu_a1|{CODE}|another_model": 1}


class TestTheRegisteredToolsRefuse:
    @pytest.fixture
    def the_printer(self, bambu, monkeypatch):
        import kiln.server as srv

        asks = _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "garage"))
        monkeypatch.setattr(srv, "_resolve_effective_printer_name", lambda name=None: "garage")
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_is_heater_watchdog_machine", lambda adapter: False)
        monkeypatch.setattr(srv, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_check_confirmation", lambda *a, **k: None)
        monkeypatch.setattr(srv, "_CONFIRM_MODE", False)
        return bambu, asks

    @pytest.mark.parametrize("tool", ["home_axes", "park_head", "load_filament", "unload_filament", "purge_filament", "wipe_nozzle"])
    def test_the_tool_answers_with_the_code_and_sends_nothing(self, the_printer, tool):
        from kiln.plugins import filament_handling_tools, homing_tools

        printer, asks = the_printer
        fn = getattr(homing_tools, tool, None) or getattr(filament_handling_tools, tool)
        kwargs = {} if tool in ("home_axes", "park_head") else {"material": "PLA"}
        if tool == "load_filament":
            kwargs["slot"] = 0
        out = fn(**kwargs)
        assert out["success"] is False and out["error"]["code"] == CODE
        assert "reports itself as the Bambu Lab X2D" in out["error"]["message"]
        assert _published(printer) == [] and asks == []

    def test_kiln_home_and_kiln_park_refuse_at_the_terminal(self, the_printer):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        printer, asks = the_printer
        for command in ("home", "park"):
            result = CliRunner().invoke(cli, [command])
            said = " ".join(result.output.replace("│", " ").split())  # the terminal wraps the sentence in a box
            assert result.exit_code == 1 and "reports itself as the Bambu Lab X2D" in said
        assert _published(printer) == [] and asks == []


class TestOnlyAProvenDisagreementRefuses:
    """Each of these runs the home it was asked for."""

    def _homes(self, bambu) -> bool:
        result = bambu.home_axes()
        return bool(result.success) and any("G28" in script for script in _scripts(bambu))

    def test_a_printer_that_is_the_model_it_was_set_up_as(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial = A1
        assert self._homes(bambu)

    def test_the_same_model_spelled_the_way_its_maker_writes_it(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial = A1
        bambu._printer_model = "bambu lab a1"
        assert self._homes(bambu)

    @pytest.mark.parametrize("serial", [UNLISTED, "", "20"])
    def test_a_serial_number_kiln_has_no_model_for(self, bambu, monkeypatch, serial):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial = serial
        assert self._homes(bambu)

    def test_a_serial_number_and_a_firmware_name_that_disagree_with_each_other(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._fw_modules = [{"name": "ota", "product_name": "Bambu Lab P1S"}]
        assert self._homes(bambu)

    def test_an_owner_who_said_it_is_the_model_it_is_set_up_as(self, bambu, monkeypatch, saved):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        _owner_said(saved, serial=X2D.lower(), model="bambu_a1")
        assert self._homes(bambu)

    @pytest.mark.parametrize(
        ("said", "why"),
        [
            ({"serial": A1, "model": "bambu_a1"}, "the word is another machine's"),
            ({"serial": X2D, "model": "bambu_p1s"}, "the word names another model"),
            ({"serial": A1, "model": "bambu_a1", "statement_serial": X2D}, "written on another printer's record"),
        ],
    )
    def test_a_word_that_is_not_for_this_machine_and_model_does_not_count(self, bambu, monkeypatch, saved, said, why):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        _owner_said(saved, **said)
        with pytest.raises(PrinterError, match="reports itself as the Bambu Lab X2D"):
            bambu.home_axes()

    @pytest.mark.parametrize("model", ["", "bambu_x1", "widgetco_9000"])
    def test_no_model_or_one_outside_the_catalogue_is_answered_as_before(self, bambu, monkeypatch, model):
        """Not this refusal: these printers keep the answer they already had."""
        from kiln.printer_model_check import move_refusal

        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._printer_model = model
        assert move_refusal(bambu) is None

    def test_limits_set_for_another_model_with_no_model_to_plan_moves_for(self, bambu, monkeypatch):
        """A purge with no model to plan a move for runs where the head is."""
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._printer_model = ""
        bambu.set_safety_profile("kilntest_open")
        from kiln.printer_model_check import move_refusal

        assert move_refusal(bambu) is None

    def test_a_backend_whose_report_is_a_name_it_was_given_is_not_held(self, monkeypatch):
        """An OctoPrint printer reporting another model still homes: its report is not proof."""
        adapter = _build("octoprint")
        adapter._printer_model = "bambu_a1"
        monkeypatch.setattr(adapter, "get_printer_info", lambda: PrinterInfo(model="bambu_x2d", raw_model="X2D", source="test"))
        monkeypatch.setattr(adapter, "get_state", lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=25.0))
        sent: list[list[str]] = []
        monkeypatch.setattr(adapter, "send_gcode", lambda cmds: sent.append(cmds) or CommandVerdict.accepted_only("queued"))
        assert adapter.home_axes(plate_clear=True).success and sent == [["G28"]]

    def test_only_the_bambu_backend_is_held_today(self):
        from .test_filament_handling import _all_adapter_classes

        held = sorted(name for name, cls in _all_adapter_classes().items() if cls._MODEL_REPORT_HOLDS_MOVES)
        assert held == ["bambu"] and PrinterAdapter._MODEL_REPORT_HOLDS_MOVES is False


class TestTheEnclosureKit:
    """The maker sells a kit that makes a P1P a P1S; the printer's serial number stays a P1P's."""

    def _p1p_set_up_as(self, bambu, monkeypatch, model):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial = P1P
        bambu._printer_model = model
        bambu._kiln_registered_name = "garage"

    def test_without_the_kit_recorded_it_is_refused_and_the_kit_is_named(self, bambu, monkeypatch):
        self._p1p_set_up_as(bambu, monkeypatch, "bambu_p1s")
        with pytest.raises(PrinterError) as refused:
            bambu.home_axes()
        said = str(refused.value)
        assert "reports itself as the Bambu Lab P1P" in said and "set-model bambu_p1p --printer garage --replace" in said
        assert "If the Bambu Lab P1P Enclosure Kit is fitted, its owner can record that with" in said
        assert "set-model bambu_p1s --printer garage --owner-says-so" in said
        assert _published(bambu) == []

    def test_with_the_kit_recorded_the_moves_run(self, bambu, monkeypatch, saved):
        self._p1p_set_up_as(bambu, monkeypatch, "bambu_p1s")
        _owner_said(saved, serial=P1P, model="bambu_p1s", upgrade=KIT)
        assert bambu.home_axes().success

    def test_the_plan_ask_says_the_kit_is_fitted(self, bambu, monkeypatch, saved):
        from kiln import _pro_motion_bridge as bridge

        self._p1p_set_up_as(bambu, monkeypatch, "bambu_p1s")
        assert "upgrade" not in bridge._machine_request(bambu, "home", "XYZ", False)
        _owner_said(saved, serial=P1P, model="bambu_p1s", upgrade=KIT)
        assert bridge._machine_request(bambu, "home", "XYZ", False)["upgrade"] == KIT

    def test_a_p1s_set_up_as_a_p1p_is_told_to_set_it_as_a_p1s(self, bambu, monkeypatch):
        self._p1p_set_up_as(bambu, monkeypatch, "bambu_p1p")
        bambu._serial = P1S
        with pytest.raises(PrinterError) as refused:
            bambu.home_axes()
        said = str(refused.value)
        assert "reports itself as the Bambu Lab P1S" in said and "set-model bambu_p1s --printer garage" in said
        assert "Enclosure Kit" not in said


class TestTheUpgradeList:
    """One data file lists the upgrades printer makers sell; a new row needs no code."""

    def test_every_row_in_the_file_is_well_formed(self):
        import json

        from kiln.data_manifest import DATA_ROOT
        from kiln.printer_model_check import maker_upgrades

        raw = json.loads((DATA_ROOT / "maker_upgrades.json").read_text(encoding="utf-8"))
        assert sorted(maker_upgrades()) == sorted(k for k in raw if not k.startswith("_"))

    def test_each_row_that_becomes_another_unreported_model_takes_the_owners_word(self):
        from kiln.printer_model_check import _row, maker_upgrades, upgrade_between

        for upgrade, row in maker_upgrades().items():
            fitted_to, becomes = _row(row["fitted_to"]), _row(row.get("becomes"))
            assert fitted_to is not None, f"{upgrade}: {row['fitted_to']} is not a catalogue model"
            if row.get("becomes"):
                assert becomes is not None, f"{upgrade}: {row['becomes']} is not a catalogue model"
            takes_a_word = bool(becomes) and row["reports_itself"] is not True and set(row["follows"].values()) == {"becomes"}
            assert (upgrade_between(fitted_to, becomes) == upgrade) is takes_a_word, upgrade

    @pytest.fixture
    def rows(self, tmp_path, monkeypatch):
        import json

        from kiln import data_manifest, printer_model_check

        def _write(table):
            (tmp_path / "maker_upgrades.json").write_text(json.dumps(table), encoding="utf-8")
            monkeypatch.setattr(data_manifest, "DATA_ROOT", tmp_path)
            printer_model_check.maker_upgrades.cache_clear()

        yield _write
        printer_model_check.maker_upgrades.cache_clear()

    def test_a_row_of_any_other_shape_is_left_out(self, rows):
        from kiln.printer_model_check import maker_upgrades

        good = {"name": "a kit", "fitted_to": "bambu_p1p", "becomes": "bambu_p1s", "reports_itself": False,
                "follows": {"slicing": "becomes", "limits": "becomes", "moves": "becomes"}}
        rows({
            "good": good,
            "both": {**good, "adds": "a lid"},
            "unsure": {**good, "reports_itself": "maybe"},
            "no_moves": {**good, "follows": {"slicing": "becomes", "limits": "becomes"}},
            "no_name": {**good, "name": ""},
        })
        assert sorted(maker_upgrades()) == ["good"]

    def test_an_upgrade_the_printer_reports_itself_takes_no_word(self, bambu, monkeypatch, saved, rows):
        """The machine's own report wins: a word saying otherwise is not taken, and none is offered."""
        rows({KIT: {"name": "the kit", "fitted_to": "bambu_p1p", "becomes": "bambu_p1s", "reports_itself": True,
                    "follows": {"slicing": "becomes", "limits": "becomes", "moves": "becomes"}}})
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        bambu._serial, bambu._printer_model = P1P, "bambu_p1s"
        _owner_said(saved, serial=P1P, model="bambu_p1s", upgrade=KIT)
        with pytest.raises(PrinterError) as refused:
            bambu.home_axes()
        assert "reports itself as the Bambu Lab P1P" in str(refused.value)
        assert "can record that" not in str(refused.value)


class TestStoppingAndReadingAreNeverAsked:
    @pytest.mark.parametrize(
        ("door", "command"),
        [
            ("pause_print", "pause"),
            ("cancel_print", "stop"),
            ("emergency_stop", "gcode_line"),
        ],
    )
    def test_the_command_reaches_the_printer(self, bambu, monkeypatch, door, command):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        getattr(bambu, door)()
        assert command in [p.get("print", {}).get("command") for p in _published(bambu)]

    def test_status_is_read(self, bambu, monkeypatch):
        bambu._serial = X2D
        bambu._printer_model = "bambu_a1"
        assert bambu.get_state().connected is True


class TestSettingTheModel:
    @pytest.fixture
    def config(self, saved):
        saved.write_text(yaml.safe_dump({
            "active_printer": "garage",
            "printers": {
                "garage": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": X2D},
                "attic": {"type": "bambu", "host": "192.168.1.8", "access_code": "12345678", "serial": UNLISTED},
                "loft": {"type": "bambu", "host": "192.168.1.7", "access_code": "12345678", "serial": P1P},
            },
        }), encoding="utf-8")
        return saved

    def _entry(self, config, name="garage"):
        return yaml.safe_load(config.read_text(encoding="utf-8"))["printers"][name]

    def test_a_model_the_serial_number_contradicts_is_not_written(self, config):
        from kiln.printer_setup import set_printer_model

        for replace in (False, True):
            out = set_printer_model("Bambu Lab A1", replace=replace, config_path=config)
            assert (out["success"], out["code"]) == (False, "MODEL_DISAGREES_WITH_PRINTER")
            assert "serial number is the Bambu Lab X2D's" in out["error"]
            assert "kiln set-model bambu_x2d --printer garage" in out["error"]
            assert "its owner can say so with" in out["error"] and "KILN_" not in out["error"]
        assert "printer_model" not in self._entry(config)

    def test_the_command_the_refusals_name_sets_the_model(self, config):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        done = CliRunner().invoke(cli, ["set-model", "bambu_x2d", "--printer", "garage", "--replace"])
        assert done.exit_code == 0, done.output
        assert self._entry(config)["printer_model"] == "bambu_x2d"

    def test_the_terminal_refuses_it_too(self, config):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        refused = CliRunner().invoke(cli, ["set-model", "Bambu Lab A1", "--replace"])
        assert refused.exit_code != 0 and "serial number is the Bambu Lab X2D's" in refused.output
        assert "printer_model" not in self._entry(config)

    def test_the_tool_refuses_it_too(self, config, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        out = srv.set_printer_model("Bambu Lab A1", replace=True)
        assert out["success"] is False and out["code"] == "MODEL_DISAGREES_WITH_PRINTER"
        assert "printer_model" not in self._entry(config)

    def test_a_serial_number_kiln_has_no_model_for_takes_the_model_named(self, config):
        from kiln.printer_setup import set_printer_model

        out = set_printer_model("Bambu Lab A1", printer_name="attic", config_path=config)
        assert out["applied"] and self._entry(config, "attic")["printer_model"] == "bambu_a1"

    def test_a_said_by_no_door_proved_writes_nothing(self, config):
        from kiln.printer_setup import set_printer_model

        out = set_printer_model("Bambu Lab A1", said_by={"how": "agent", "by": "me"}, config_path=config)
        assert out["code"] == "OWNER_MUST_SAY_SO" and "printer_model" not in self._entry(config)


class TestTheOwnerSaysSoAtATerminal:
    @pytest.fixture
    def config(self, saved, monkeypatch):
        saved.write_text(yaml.safe_dump({
            "active_printer": "garage",
            "printers": {
                "garage": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": X2D},
                "loft": {"type": "bambu", "host": "192.168.1.7", "access_code": "12345678", "serial": P1P},
            },
        }), encoding="utf-8")
        monkeypatch.setattr("kiln.consent_windows.person_at_terminal", lambda: True)
        return saved

    def _run(self, *args, typed=""):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        return CliRunner().invoke(cli, ["set-model", *args, "--owner-says-so"], input=typed)

    def _entry(self, config, name="garage"):
        return yaml.safe_load(config.read_text(encoding="utf-8"))["printers"][name]

    def test_the_owner_reads_it_types_the_printers_name_and_it_is_recorded(self, config):
        done = self._run("Bambu Lab A1", "--printer", "garage", typed="garage\n")
        assert done.exit_code == 0, done.output
        assert "You are about to record that garage is the Bambu Lab A1, whatever its serial number says." in done.output
        entry = self._entry(config)
        assert entry["printer_model"] == "bambu_a1"
        said = entry["owner_says"]
        assert (said["serial"], said["model"], said["how"]) == (X2D, "bambu_a1", "user_terminal")
        assert "upgrade" not in said and said["by"].startswith("os_user:") and said["said_at"]

    def test_the_kit_is_recorded_the_same_way(self, config):
        done = self._run("bambu_p1s", "--printer", "loft", typed="loft\n")
        assert done.exit_code == 0, done.output
        assert "the Bambu Lab P1P Enclosure Kit is fitted to loft, so it is the Bambu Lab P1S" in done.output
        said = self._entry(config, "loft")["owner_says"]
        assert (said["model"], said["upgrade"]) == ("bambu_p1s", KIT)

    def test_another_name_typed_records_nothing(self, config):
        done = self._run("Bambu Lab A1", "--printer", "garage", typed="loft\n")
        assert done.exit_code != 0 and "nothing was recorded" in done.output
        assert "printer_model" not in self._entry(config) and "owner_says" not in self._entry(config)

    def test_with_nobody_at_the_terminal_nothing_is_recorded(self, config, monkeypatch):
        monkeypatch.setattr("kiln.consent_windows.person_at_terminal", lambda: False)
        done = self._run("Bambu Lab A1", "--printer", "garage", typed="garage\n")
        assert done.exit_code != 0 and "owner can say this, at a terminal" in done.output
        assert "owner_says" not in self._entry(config)

    def test_setting_it_to_what_it_reports_takes_the_word_back(self, config):
        from kiln.printer_setup import set_printer_model

        assert self._run("Bambu Lab A1", "--printer", "garage", typed="garage\n").exit_code == 0
        out = set_printer_model("bambu_x2d", printer_name="garage", replace=True, config_path=config)
        assert out["applied"]
        assert self._entry(config)["printer_model"] == "bambu_x2d" and "owner_says" not in self._entry(config)

    def test_setting_the_same_model_again_keeps_the_word(self, config):
        from kiln.printer_setup import set_printer_model

        assert self._run("Bambu Lab A1", "--printer", "garage", typed="garage\n").exit_code == 0
        set_printer_model("bambu_a1", printer_name="garage", config_path=config)
        assert self._entry(config)["owner_says"]["model"] == "bambu_a1"


class TestTheOwnerSaysSoWithACode:
    @pytest.fixture
    def door(self, saved, monkeypatch):
        import kiln.server as srv
        from kiln import screen_code
        from kiln.plugins.safety_tools import _SafetyToolsPlugin

        saved.write_text(yaml.safe_dump({"active_printer": "garage", "printers": {
            "garage": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": X2D},
        }}), encoding="utf-8")
        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        screen_code._reset_for_tests()
        shown: list = []
        monkeypatch.setattr(screen_code, "_show_hook", lambda issued: shown.append(issued) or True)
        monkeypatch.setattr(screen_code, "screen_available", lambda: True)
        monkeypatch.setattr(screen_code, "MIN_READ_S", 0.0)
        tools: dict = {}

        class _Mcp:
            def tool(self, **_kwargs):
                return lambda fn: tools.setdefault(fn.__name__, fn)

        _SafetyToolsPlugin().register(_Mcp())
        yield srv, tools, shown
        screen_code._reset_for_tests()

    def _entry(self, saved):
        return yaml.safe_load(saved.read_text(encoding="utf-8"))["printers"]["garage"]

    def test_only_the_code_the_person_types_records_it(self, door, saved):
        from kiln import screen_code

        srv, tools, shown = door
        first = srv.set_printer_model("Bambu Lab A1", owner_says_so=True)
        assert first["success"] is False and first["error"]["code"] == "CONFIRMATION_REQUIRED"
        assert "owner_says" not in self._entry(saved) and "printer_model" not in self._entry(saved)
        assert len(shown) == 1
        _title, subtitle, message = screen_code.banner_text(shown[0])
        assert subtitle == "Garage is the Bambu Lab A1, whatever its serial number says"
        assert message.startswith("Only if that is true")

        relayed = tools["give_print_code"](shown[0].code)
        assert relayed["success"] is True and "set_printer_model" in relayed["next"]
        second = srv.set_printer_model("Bambu Lab A1", owner_says_so=True)
        assert second["success"] is True, second
        said = self._entry(saved)["owner_says"]
        assert (said["model"], said["how"]) == ("bambu_a1", "user_code")

    def test_a_wrong_code_records_nothing(self, door, saved):
        srv, tools, shown = door
        srv.set_printer_model("Bambu Lab A1", owner_says_so=True)
        wrong = "0000" if shown[0].code != "0000" else "1111"
        assert tools["give_print_code"](wrong)["success"] is False
        assert srv.set_printer_model("Bambu Lab A1", owner_says_so=True)["success"] is False
        assert "owner_says" not in self._entry(saved)

    def test_a_print_code_does_not_record_it(self, door, saved):
        from kiln import screen_code

        srv, tools, shown = door
        screen_code.issue(tool="start_print", file_name="model statement for garage", file_sha256="", printer_name="garage")
        assert shown[-1].tool == "start_print"
        tools["give_print_code"](shown[-1].code)
        assert srv.set_printer_model("Bambu Lab A1", owner_says_so=True)["success"] is False
        assert "owner_says" not in self._entry(saved)


class TestAStatementDoesNotOutliveWhatItWasAbout:
    """A statement is about one model on one machine; a later change of either leaves it unread."""

    def _entry(self, saved, name="garage"):
        return yaml.safe_load(saved.read_text(encoding="utf-8"))["printers"][name]

    def _resave(self, saved, **changes):
        from kiln.cli.config import save_printer

        fields = {"access_code": "12345678", "serial": X2D, "printer_model": "bambu_a1", **changes}
        save_printer("garage", "bambu", "192.168.1.9", config_path=saved, **fields)

    def test_saving_the_printer_as_another_model_drops_the_statement(self, saved):
        """The writer register_printer, `kiln add-printer`, the setup wizard and the bridge share."""
        _owner_said(saved, serial=X2D, model="bambu_a1")
        self._resave(saved, printer_model="bambu_x1c")
        assert self._entry(saved)["printer_model"] == "bambu_x1c" and "owner_says" not in self._entry(saved)

    def test_saving_it_again_as_the_same_machine_and_model_keeps_it(self, saved):
        _owner_said(saved, serial=X2D, model="bambu_a1")
        self._resave(saved)
        assert self._entry(saved)["owner_says"]["model"] == "bambu_a1"

    def test_a_statement_left_beside_another_model_is_not_read(self, bambu, monkeypatch, saved):
        """A hand edit, or a running Kiln that has not reread its settings: the record says
        X1C, the old statement says A1, and the machine Kiln is driving still says A1."""
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        _owner_said(saved, serial=X2D, model="bambu_a1")
        raw = yaml.safe_load(saved.read_text(encoding="utf-8"))
        raw["printers"]["garage"]["printer_model"] = "bambu_x1c"
        saved.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(PrinterError, match="reports itself as the Bambu Lab X2D"):
            bambu.home_axes()

    def test_set_model_does_not_carry_a_statement_to_a_model_edited_in_by_hand(self, saved):
        from kiln.printer_setup import set_printer_model

        _owner_said(saved, serial=X2D, model="bambu_a1")
        raw = yaml.safe_load(saved.read_text(encoding="utf-8"))
        raw["printers"]["garage"]["printer_model"] = "bambu_x1c"
        saved.write_text(yaml.safe_dump(raw), encoding="utf-8")
        out = set_printer_model("bambu_a1", printer_name="garage", replace=True, config_path=saved)
        assert out["code"] == "MODEL_DISAGREES_WITH_PRINTER"


def test_the_setup_check_shows_the_owners_word(bambu, monkeypatch, saved):
    from kiln.printer_model_check import statement_line

    _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
    assert statement_line(bambu, "garage") is None
    _owner_said(saved, serial=X2D, model="bambu_a1")
    line = statement_line(bambu, "garage")
    assert "its owner said on 2026-11-19, at a terminal, that garage is the Bambu Lab A1" in line
    assert "Setting its model to what it reports takes that back." in line


class TestTheSettlingCommand:
    def test_replacing_a_known_model_is_asked_for_by_name(self):
        from kiln.printer_model_check import ModelCheck

        known = ModelCheck("differs", "bambu_a1", "bambu_a1", "bambu_x2d")
        assert "set-model bambu_x2d --printer garage --replace" in known.fix("garage")
        unknown = ModelCheck("differs", "acme_mark_3", None, "bambu_x2d")
        assert "--replace" not in unknown.fix("garage")


def test_a_serial_number_names_its_model():
    from kiln.printers.bambu import model_of_serial

    assert model_of_serial(X2D) == "bambu_x2d" and model_of_serial(A1.lower()) == "bambu_a1"
    assert model_of_serial(UNLISTED) is None and model_of_serial("") is None and model_of_serial(None) is None


def test_the_move_tools_carry_no_new_argument():
    """The check asks nothing of the caller: no door grew a way to switch it off."""
    import inspect

    from kiln.plugins import filament_handling_tools, homing_tools

    for module, names in ((homing_tools, ("home_axes", "park_head")),
                          (filament_handling_tools, ("load_filament", "unload_filament", "purge_filament", "wipe_nozzle"))):
        for name in names:
            params = inspect.signature(getattr(module, name)).parameters
            assert not [p for p in params if "model" in p or "confirm" in p or "force" in p], (name, list(params))




class TestWhatAPlanAskSends:
    """The serial number leaves this computer only as a one-way code, so the ask
    says which model the machine names itself, as a catalogue model id."""

    @pytest.fixture
    def wire(self, monkeypatch):
        import json
        import urllib.request

        import kiln.server as srv
        from kiln import _pro_motion_bridge as bridge

        sent: list[dict] = []

        class _Refused:
            status = 200

            def read(self):
                return json.dumps({"status": "error", "code": CODE, "error": "no"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: sent.append(json.loads(req.data)) or _Refused())
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("token", None))
        monkeypatch.setattr(bridge, "_local_pro", lambda: None)
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        monkeypatch.setattr(bridge, "_misses", {})
        return bridge, sent

    def test_the_ask_names_the_model_and_carries_no_serial_number(self, bambu, wire):
        bridge, sent = wire
        bambu._serial, bambu._printer_model = X2D, "bambu_a1"
        assert bridge.plan_for(bambu, "wipe", axes="XY") is None
        body = sent[-1]
        assert body["reported_model"] == "bambu_x2d" and body["printer_id"] == "bambu_a1"
        assert body["serial"].startswith("id:") and X2D not in str(body)

    def test_the_kit_rides_with_the_ask(self, bambu, wire, saved):
        bridge, sent = wire
        bambu._serial, bambu._printer_model = P1P, "bambu_p1s"
        _owner_said(saved, serial=P1P, model="bambu_p1s", upgrade=KIT)
        bridge.plan_for(bambu, "wipe", axes="XY")
        assert (sent[-1]["reported_model"], sent[-1]["upgrade"]) == ("bambu_p1p", KIT)

    @pytest.mark.parametrize(
        "body",
        [
            {"reported_model": X2D.lower()},
            {"reported_model": "printer at 10.0.0.5"},
            {"upgrade": "a kit nobody listed"},
            {"upgrade": X2D},
        ],
    )
    def test_the_guard_lets_only_a_listed_name_through_under_those_keys(self, body):
        from kiln.outbound_guard import scrub

        assert scrub(body) == {}

    def test_listed_names_go_as_they_are(self):
        from kiln.outbound_guard import scrub

        body = {"reported_model": "bambu_x2d", "upgrade": KIT}
        assert scrub(body) == body
