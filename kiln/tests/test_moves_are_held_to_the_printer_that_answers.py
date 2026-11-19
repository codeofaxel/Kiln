"""A move Kiln plans by model is refused while the printer names another model.

Kiln sends its own head moves for a home, a park, a wipe, a purge and the
park before a filament change, and plans them for the model the printer was
set up as.  A Bambu Lab printer says what it is in its serial number and its
firmware name, so every one of those doors refuses when that answer names a
different catalogue printer, and says the one command that settles it.

The refusal needs proof.  A serial number Kiln has no model for, a printer
with no model set, a model outside the catalogue, two answers from the
printer that disagree with each other, and an owner who has said the model
stands all let the move run.  Stopping, pausing, cancelling and reading the
printer are never asked.  Setting the model is refused the same way.

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
UNLISTED = "ZZZ00A000000001"

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
def _own_records(tmp_path, monkeypatch):
    """Each test has its own plate record, tally file and no owner's word."""
    from kiln import daily_stats

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    monkeypatch.delenv("KILN_DECLARED_MODEL_STANDS_FOR", raising=False)


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

    def test_the_refusal_does_not_say_how_to_keep_the_wrong_model(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        with pytest.raises(PrinterError) as refused:
            bambu.park_head()
        assert "KILN_" not in str(refused.value)
        assert bambu._serial not in str(refused.value)

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

    def test_an_owner_who_has_said_the_model_stands_for_this_printer(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        monkeypatch.setenv("KILN_DECLARED_MODEL_STANDS_FOR", f"0000, {X2D.lower()}")
        assert self._homes(bambu)

    def test_the_owners_word_for_another_printer_does_not_carry_over(self, bambu, monkeypatch):
        _an_x2d_set_up_as_an_a1(bambu, monkeypatch)
        monkeypatch.setenv("KILN_DECLARED_MODEL_STANDS_FOR", A1)
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
    def config(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({
            "active_printer": "garage",
            "printers": {
                "garage": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": X2D},
                "attic": {"type": "bambu", "host": "192.168.1.8", "access_code": "12345678", "serial": UNLISTED},
            },
        }), encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: path)
        monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", path)
        monkeypatch.setattr("kiln.printer_model_resolver._cache", (0.0, None))
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        return path

    def _model(self, config, name="garage"):
        return yaml.safe_load(config.read_text(encoding="utf-8"))["printers"][name].get("printer_model")

    def test_a_model_the_serial_number_contradicts_is_not_written(self, config):
        from kiln.printer_setup import set_printer_model

        for replace in (False, True):
            out = set_printer_model("Bambu Lab A1", replace=replace, config_path=config)
            assert (out["success"], out["code"]) == (False, "MODEL_DISAGREES_WITH_PRINTER")
            assert "serial number is the Bambu Lab X2D's" in out["error"]
            assert "kiln set-model bambu_x2d --printer garage" in out["error"]
            assert "KILN_" not in out["error"]
        assert self._model(config) is None

    def test_the_command_the_refusals_name_sets_the_model(self, config):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        done = CliRunner().invoke(cli, ["set-model", "bambu_x2d", "--printer", "garage", "--replace"])
        assert done.exit_code == 0, done.output
        assert self._model(config) == "bambu_x2d"

    def test_the_terminal_refuses_it_too(self, config):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        refused = CliRunner().invoke(cli, ["set-model", "Bambu Lab A1", "--replace"])
        assert refused.exit_code != 0 and "serial number is the Bambu Lab X2D's" in refused.output
        assert self._model(config) is None

    def test_the_tool_refuses_it_too(self, config, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
        out = srv.set_printer_model("Bambu Lab A1", replace=True)
        assert out["success"] is False and out["code"] == "MODEL_DISAGREES_WITH_PRINTER"
        assert self._model(config) is None

    def test_a_serial_number_kiln_has_no_model_for_takes_the_model_named(self, config):
        from kiln.printer_setup import set_printer_model

        out = set_printer_model("Bambu Lab A1", printer_name="attic", config_path=config)
        assert out["applied"] and self._model(config, "attic") == "bambu_a1"

    def test_an_owner_who_has_said_the_model_stands_is_taken_at_their_word(self, config, monkeypatch):
        from kiln.printer_setup import set_printer_model

        monkeypatch.setenv("KILN_DECLARED_MODEL_STANDS_FOR", X2D)
        out = set_printer_model("Bambu Lab A1", config_path=config)
        assert out["applied"] and self._model(config) == "bambu_a1"


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


