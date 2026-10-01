"""A saved printer's model is set through one door, by name or from a slicer file.

Kiln reads the bed it checks a print against, the temperatures it allows and
the profile it slices with from the printer's ``printer_model``.  With none
set, the checks are skipped -- and until this door the only way to set one
was to edit ``config.yaml`` by hand, in the catalogue's spelling.  Twelve
messages told people to do that, and two named a ``set_printer_model`` tool
that did not exist.

These pin the door: a model named in any spelling the catalogue knows, or
read out of a project saved from a slicer; one field of one entry written;
nothing written for a model the catalogue does not hold, one that does not
suit the connection, or over a different model unasked.  The fixtures are in
the shape each slicer writes.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
import yaml

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
from kiln.printer_setup import (
    AGENT_REMEDY,
    CLI_REMEDY,
    SetupFileError,
    read_slicer_setup,
    set_printer_model,
)

# ---------------------------------------------------------------------------
# Slicer files, in the shape each slicer writes
# ---------------------------------------------------------------------------

_BAMBU = {
    "printer_model": "Bambu Lab A1",
    "printer_settings_id": "Bambu Lab A1 0.4 nozzle",
    "printer_variant": "0.4",
    "nozzle_diameter": ["0.4"],
    "nozzle_type": ["stainless_steel"],
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "printable_height": "256",
    "filament_type": ["PLA", "PLA"],
}

_PRUSA = """\
; bed_shape = 0x0,250x0,250x210,0x210
; filament_type = PETG
; max_print_height = 220
; nozzle_diameter = 0.4
; printer_model = MK4IS
; printer_settings_id = Original Prusa MK4 Input Shaper 0.4 nozzle
"""


def _bambu_project(path: Path, **changes) -> str:
    """A project as Bambu Studio and OrcaSlicer save it: one JSON object."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", json.dumps({**_BAMBU, **changes}))
    return str(path)


def _prusa_project(path: Path, text: str = _PRUSA) -> str:
    """A project as PrusaSlicer saves it: ``; key = value`` lines."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/Slic3r_PE.config", text)
    return str(path)


@pytest.fixture
def config(tmp_path, monkeypatch) -> Path:
    """Two saved printers and no model on either, as Kiln's own config file."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "active_printer": "garage",
        "printers": {
            "garage": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": "039X"},
            "shed": {"type": "moonraker", "host": "http://shed.local"},
        },
    }), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: path)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", path)
    monkeypatch.setattr("kiln.printer_model_resolver._cache", (0.0, None))
    return path


@pytest.fixture(autouse=True)
def _no_nozzle_known(monkeypatch):
    monkeypatch.setattr(assumed, "_setting_memo", {})
    monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": None, "answered": True})
    monkeypatch.setattr(bridge, "consult_only_recorded_nozzle", lambda: {"printer_id": None, "diameter_mm": None, "answered": True})
    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)


def _saved(config: Path) -> dict:
    return yaml.safe_load(config.read_text(encoding="utf-8"))["printers"]


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


class TestWhatASlicerFileSays:
    def test_a_bambu_or_orca_project(self, tmp_path):
        setup = read_slicer_setup(_bambu_project(tmp_path / "part.3mf"))
        assert setup.printer == "Bambu Lab A1"
        assert (setup.nozzle_mm, setup.nozzle_material) == (0.4, "stainless_steel")
        assert setup.bed_mm == (256.0, 256.0, 256.0)
        assert setup.material == "PLA"

    def test_a_prusaslicer_project(self, tmp_path):
        setup = read_slicer_setup(_prusa_project(tmp_path / "part.3mf"))
        assert setup.printer == "MK4IS"
        assert setup.bed_mm == (250.0, 210.0, 220.0)
        assert (setup.nozzle_mm, setup.material) == (0.4, "PETG")

    def test_an_exported_prusaslicer_config(self, tmp_path):
        exported = tmp_path / "config.ini"
        exported.write_text(_PRUSA.replace("; ", ""), encoding="utf-8")
        assert read_slicer_setup(str(exported)).printer == "MK4IS"

    def test_an_exported_orca_printer_preset(self, tmp_path):
        preset = tmp_path / "printer.json"
        preset.write_text(json.dumps(_BAMBU), encoding="utf-8")
        assert read_slicer_setup(str(preset)).bed_mm == (256.0, 256.0, 256.0)

    def test_a_project_with_no_printer_chosen_names_none(self, tmp_path):
        setup = read_slicer_setup(_bambu_project(tmp_path / "part.3mf", printer_model="", printer_settings_id=""))
        assert setup.printer is None and setup.nozzle_mm == 0.4

    def test_the_preset_name_answers_when_the_model_is_blank(self, tmp_path):
        setup = read_slicer_setup(_bambu_project(tmp_path / "part.3mf", printer_model=""))
        assert setup.printer == "Bambu Lab A1 0.4 nozzle"

    def test_nozzles_that_differ_give_no_one_size(self, tmp_path):
        setup = read_slicer_setup(_bambu_project(tmp_path / "part.3mf", nozzle_diameter=["0.4", "0.6"]))
        assert setup.nozzle_mm is None

    def test_a_bare_model_is_refused_with_what_to_send_instead(self, tmp_path):
        bare = tmp_path / "bare.3mf"
        with zipfile.ZipFile(bare, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        with pytest.raises(SetupFileError, match="holds no slicer settings.*Save Project"):
            read_slicer_setup(str(bare))

    def test_a_missing_file_and_a_broken_one_are_refused(self, tmp_path):
        with pytest.raises(SetupFileError, match="No file"):
            read_slicer_setup(str(tmp_path / "gone.3mf"))
        broken = tmp_path / "broken.3mf"
        broken.write_bytes(b"not a zip")
        with pytest.raises(SetupFileError, match="holds no slicer settings"):
            read_slicer_setup(str(broken))


# ---------------------------------------------------------------------------
# Setting the model
# ---------------------------------------------------------------------------


class TestTheModelIsSet:
    def test_from_a_slicer_file_onto_the_active_printer(self, config, tmp_path):
        out = set_printer_model(slicer_file=_bambu_project(tmp_path / "part.3mf"), config_path=config)
        assert (out["success"], out["applied"], out["printer"], out["printer_model"]) == (True, True, "garage", "bambu_a1")
        assert _saved(config)["garage"]["printer_model"] == "bambu_a1"
        assert out["file"]["bed_mm"] == [256.0, 256.0, 256.0]
        assert out["message"].startswith("garage is now set up as bambu_a1.")

    @pytest.mark.parametrize(("said", "key"), [("Bambu Lab A1", "bambu_a1"), ("bambu_a1", "bambu_a1"), ("bambu-a1", "bambu_a1")])
    def test_by_name_in_any_spelling_the_catalogue_knows(self, config, said, key):
        out = set_printer_model(said, config_path=config)
        assert out["applied"] and _saved(config)["garage"]["printer_model"] == key

    def test_onto_a_named_printer(self, config):
        out = set_printer_model("Voron 2.4", printer_name="shed", config_path=config)
        assert out["applied"] and _saved(config)["shed"]["printer_model"] == "voron_2"
        assert "printer_model" not in _saved(config)["garage"]

    def test_only_that_one_field_changes(self, config):
        before = yaml.safe_load(config.read_text(encoding="utf-8"))
        set_printer_model("Bambu Lab A1", config_path=config)
        after = yaml.safe_load(config.read_text(encoding="utf-8"))
        after["printers"]["garage"].pop("printer_model")
        assert after == before

    def test_the_checks_that_were_skipped_now_have_a_model_to_read(self, config):
        """The point of the door: the resolver the bed-fit and temperature
        gates ask goes from no answer to the catalogue row, with no restart."""
        from kiln.printer_model_resolver import resolve_printer_model, resolve_printer_model_for
        from kiln.printers.bed_fit import get_build_volume

        assert resolve_printer_model() is None
        set_printer_model("Bambu Lab A1", config_path=config)
        assert resolve_printer_model() == "bambu_a1"
        assert resolve_printer_model_for("garage") == "bambu_a1"
        assert get_build_volume(resolve_printer_model()) is not None

    def test_a_write_inside_the_files_own_clock_tick_is_still_seen(self, config):
        """The resolver remembers its answer per file timestamp.  A write
        that lands in the same tick as the last read leaves the timestamp
        where it was, and the remembered "no model" would stand."""
        import os

        from kiln.printer_model_resolver import resolve_printer_model

        stamp = config.stat()
        assert resolve_printer_model() is None
        set_printer_model("Bambu Lab A1", config_path=config)
        os.utime(config, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert resolve_printer_model() == "bambu_a1"

    def test_setting_it_again_changes_nothing_and_says_so(self, config):
        set_printer_model("Bambu Lab A1", config_path=config)
        out = set_printer_model("bambu_a1", config_path=config)
        assert (out["success"], out["applied"]) == (True, False)
        assert out["message"].startswith("garage is already set up as bambu_a1.")

    def test_a_loose_spelling_already_saved_is_tidied_to_the_catalogues(self, config):
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["printers"]["garage"]["printer_model"] = "Bambu Lab A1"
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        out = set_printer_model("bambu_a1", config_path=config)
        assert out["applied"] and out["previous"] == "Bambu Lab A1"
        assert _saved(config)["garage"]["printer_model"] == "bambu_a1"


class TestNothingIsWrittenWhenItShouldNotBe:
    def _unchanged(self, config: Path, before: str) -> bool:
        return config.read_text(encoding="utf-8") == before

    def test_a_model_the_catalogue_does_not_hold(self, config):
        before = config.read_text(encoding="utf-8")
        out = set_printer_model("Sovol SV08", printer_name="shed", config_path=config)
        assert (out["success"], out["code"]) == (False, "UNKNOWN_MODEL")
        assert out["close_matches"] and "Closest in the catalogue" in out["error"]
        assert self._unchanged(config, before)

    def test_an_unknown_printer_in_a_file_still_reports_what_the_file_said(self, config, tmp_path):
        project = _bambu_project(tmp_path / "part.3mf", printer_model="Acme Printomatic 9", printer_settings_id="")
        out = set_printer_model(slicer_file=project, config_path=config)
        assert out["code"] == "UNKNOWN_MODEL" and out["recognised"] is False
        assert out["file"]["bed_mm"] == [256.0, 256.0, 256.0]

    def test_a_different_model_already_set_is_kept_unless_asked(self, config):
        set_printer_model("Bambu Lab A1", config_path=config)
        before = config.read_text(encoding="utf-8")
        out = set_printer_model("Bambu Lab P1S", config_path=config)
        assert (out["success"], out["code"], out["previous"]) == (False, "MODEL_ALREADY_SET", "bambu_a1")
        assert self._unchanged(config, before)

        replaced = set_printer_model("Bambu Lab P1S", replace=True, config_path=config)
        assert replaced["applied"] and _saved(config)["garage"]["printer_model"] == "bambu_p1s"

    @pytest.mark.parametrize(("model", "printer"), [("Bambu Lab A1", "shed"), ("Voron 2.4", "garage")])
    def test_a_model_the_connection_cannot_reach(self, config, model, printer):
        before = config.read_text(encoding="utf-8")
        out = set_printer_model(model, printer_name=printer, config_path=config)
        assert out["code"] == "MODEL_DOES_NOT_SUIT_PRINTER"
        assert self._unchanged(config, before)

    def test_a_printer_nobody_saved(self, config):
        out = set_printer_model("K1 Max", printer_name="attic", config_path=config)
        assert out["code"] == "PRINTER_NOT_FOUND" and "garage, shed" in out["error"]

    def test_a_file_with_no_printer_chosen(self, config, tmp_path):
        project = _bambu_project(tmp_path / "part.3mf", printer_model="", printer_settings_id="")
        out = set_printer_model(slicer_file=project, config_path=config)
        assert out["code"] == "NO_PRINTER_IN_FILE" and out["file"]["nozzle_mm"] == 0.4

    def test_a_file_that_is_not_a_slicers(self, config, tmp_path):
        out = set_printer_model(slicer_file=str(tmp_path / "gone.3mf"), config_path=config)
        assert out["code"] == "SETUP_FILE_UNREADABLE"

    def test_on_the_hosted_server_nothing_is_read_or_written(self, config, tmp_path, monkeypatch):
        """One config file serves every account there, and a path is a file
        on the server."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        before = config.read_text(encoding="utf-8")
        monkeypatch.setattr(
            "kiln.file_metadata.slicer_settings",
            lambda path: pytest.fail("a server-side file was opened"),
        )
        for kwargs in ({"printer_model": "Bambu Lab A1"}, {"slicer_file": "/etc/hosts"}):
            out = set_printer_model(config_path=config, **kwargs)
            assert (out["success"], out["code"]) == (False, "LOCAL_ONLY")
        assert self._unchanged(config, before)

    @pytest.mark.parametrize("kwargs", [{}, {"printer_model": "Bambu Lab A1", "slicer_file": "x.3mf"}])
    def test_neither_or_both(self, config, kwargs):
        assert set_printer_model(config_path=config, **kwargs)["code"] == "INVALID_ARGS"

    def test_with_no_printer_saved_it_says_how_to_add_one(self, tmp_path, monkeypatch):
        empty = tmp_path / "config.yaml"
        out = set_printer_model("Bambu Lab A1", config_path=empty)
        assert (out["success"], out["applied"], out["printer"]) == (True, False, None)
        assert 'register_printer(printer_model="bambu_a1")' in out["message"]
        assert not empty.exists()

    def test_several_printers_and_none_active_is_never_guessed(self, config):
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw.pop("active_printer")
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        out = set_printer_model("Voron 2.4", config_path=config)
        assert out["applied"] is False and "name which one with printer_name" in out["message"]


class TestWhatTheFileSaysBesideWhatKilnHolds:
    def test_a_bed_unlike_the_catalogues_is_said_and_changes_nothing(self, config, tmp_path):
        project = _bambu_project(
            tmp_path / "part.3mf", printable_area=["0x0", "300x0", "300x300", "0x300"], printable_height="300",
        )
        out = set_printer_model(slicer_file=project, config_path=config)
        assert out["applied"]
        assert any("300 x 300 x 300 mm printable volume" in note and "256" in note for note in out["notes"])

    def test_a_bed_that_matches_says_nothing(self, config, tmp_path):
        assert set_printer_model(slicer_file=_bambu_project(tmp_path / "part.3mf"), config_path=config)["notes"] == []

    def test_a_nozzle_unlike_the_one_kiln_slices_for_is_said(self, config, tmp_path):
        project = _bambu_project(tmp_path / "part.3mf", nozzle_diameter=["0.6"], nozzle_type=["hardened_steel"])
        out = set_printer_model(slicer_file=project, config_path=config)
        note = next(note for note in out["notes"] if "nozzle" in note)
        assert "set up for a 0.6 mm hardened steel nozzle; Kiln has 0.4 mm" in note
        assert "set_nozzle_state" in note

    def test_a_nozzle_kiln_already_has_on_record_says_nothing(self, config, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": 0.6, "answered": True})
        project = _bambu_project(tmp_path / "part.3mf", nozzle_diameter=["0.6"])
        assert set_printer_model(slicer_file=project, config_path=config)["notes"] == []


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


class TestEveryDoor:
    def test_the_agent_tool(self, config, tmp_path):
        import asyncio

        from kiln import server

        out = asyncio.run(server.mcp.call_tool(
            "set_printer_model", {"slicer_file": _prusa_project(tmp_path / "part.3mf"), "printer_name": "shed"},
        ))
        content = out[0] if isinstance(out, tuple) else out
        reply = json.loads(content[0].text)
        assert reply["applied"] is True and reply["printer_model"] == "prusa_mk4"
        assert _saved(config)["shed"]["printer_model"] == "prusa_mk4"

    def test_the_agent_tool_hands_back_a_refusal_as_one(self, config):
        import asyncio

        from kiln import server

        out = asyncio.run(server.mcp.call_tool("set_printer_model", {"printer_model": "Acme Printomatic 9"}))
        content = out[0] if isinstance(out, tuple) else out
        reply = json.loads(content[0].text)
        assert reply["success"] is False and reply["code"] == "UNKNOWN_MODEL"

    def test_the_command_line(self, config, tmp_path):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        project = _bambu_project(tmp_path / "part.3mf")
        done = CliRunner().invoke(cli, ["set-model", "--from-file", project])
        assert done.exit_code == 0, done.output
        assert "garage is now set up as bambu_a1." in done.output

        refused = CliRunner().invoke(cli, ["set-model", "Bambu Lab P1S"])
        assert refused.exit_code != 0 and "garage is set up as bambu_a1" in refused.output

        as_json = CliRunner().invoke(cli, ["set-model", "Bambu Lab P1S", "--replace", "--json"])
        assert json.loads(as_json.output)["printer_model"] == "bambu_p1s"

    def test_no_message_still_sends_anyone_to_edit_the_file_by_hand(self):
        """Twelve messages sent people to config.yaml to set the model
        themselves.  Each now names the door; a new one that sends them
        back to the file is caught here."""
        src = Path(read_slicer_setup.__code__.co_filename).resolve().parent
        by_hand = re.compile(
            r"add\s+`?printer_model:\s*<|Add printer_model to ~/\.kiln|printer_model: <value>"
            r"|[Ss]et\s+`?printer_model`?\s+(in|for this printer in)\s+(~/\.kiln/)?config\.yaml"
        )
        still = [
            f"{path.relative_to(src)}:{number}"
            for path in sorted(src.rglob("*.py"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if by_hand.search(line)
        ]
        assert not still, f"still tells someone to hand-edit the model in: {still}"
        assert "set_printer_model" in AGENT_REMEDY and "kiln set-model" in CLI_REMEDY

    def test_the_missing_model_warning_names_the_door(self, config):
        from kiln.safety_gap_warning import safety_gap_warning

        warning = safety_gap_warning()
        assert warning is not None and warning["remediation"] == AGENT_REMEDY
