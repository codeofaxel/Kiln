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
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path

import pytest
import yaml

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
import kiln.safety_profiles as sp
from kiln.printer_setup import (
    AGENT_REMEDY,
    CLI_REMEDY,
    SetupFileError,
    read_slicer_setup,
    set_printer_model,
)

_PRUSASLICER = shutil.which("prusa-slicer") or shutil.which("PrusaSlicer") or next(
    (p for p in ("/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",) if os.access(p, os.X_OK)), None,
)


def _plate(path: Path) -> str:
    """A 40 x 30 x 4 mm plate at the origin, as a binary STL."""
    v = [(0, 0, 0), (40, 0, 0), (40, 30, 0), (0, 30, 0), (0, 0, 4), (40, 0, 4), (40, 30, 4), (0, 30, 4)]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    body = b"".join(struct.pack("<12fH", 0.0, 0.0, 0.0, *v[a], *v[b], *v[c], 0) for a, b, c in faces)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(faces)) + body)
    return str(path)


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
def _own_override_store(monkeypatch, tmp_path):
    """This machine's printer overrides, in a folder of the test's own."""
    store = tmp_path / "kiln_home"
    store.mkdir()
    monkeypatch.setattr(sp, "_LOCAL_OVERRIDE_FILE", store / "local_printer_overrides.json")
    monkeypatch.setattr(sp, "_LEGACY_OVERRIDE_FILE", store / "community_profiles.json")
    monkeypatch.setattr(sp, "_LOCK_FILE", store / "locked_profiles.json")
    monkeypatch.setattr(sp, "_LOCAL_DIR", store)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()
    yield store
    sp._local_overrides_loaded = False
    sp._local_override_cache.clear()


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

    def test_a_saved_value_kiln_does_not_recognise_was_doing_nothing_and_is_replaced(self, config):
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["printers"]["garage"]["printer_model"] = "bambu_a11"
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        out = set_printer_model("Bambu Lab A1", config_path=config)
        assert out["applied"] and out["previous"] == "bambu_a11"
        assert _saved(config)["garage"]["printer_model"] == "bambu_a1"

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

    def test_an_unknown_printer_in_a_file_with_no_bed_cannot_be_set_up(self, config, tmp_path):
        """The bed is the one fact Kiln cannot do without; a file that
        names an unknown printer and no bed is a name, and is refused as one."""
        before = config.read_text(encoding="utf-8")
        project = _bambu_project(
            tmp_path / "part.3mf", printer_model="Acme Printomatic 9", printer_settings_id="",
            printable_area=[], printable_height="",
        )
        out = set_printer_model(slicer_file=project, config_path=config)
        assert out["code"] == "UNKNOWN_MODEL" and out["recognised"] is False
        assert out["file"]["printer"] == "Acme Printomatic 9" and out["file"]["bed_mm"] is None
        assert self._unchanged(config, before) and sp.list_local_printer_overrides() == []

    def test_an_unknown_printer_by_name_is_told_what_would_set_it_up(self, config):
        out = set_printer_model("Acme Printomatic 9", config_path=config)
        assert out["code"] == "UNKNOWN_MODEL" and "project saved from your slicer" in out["error"]
        assert sp.list_local_printer_overrides() == []

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


# ---------------------------------------------------------------------------
# A printer outside the catalogue
# ---------------------------------------------------------------------------

ACME = "custom_acme_printomatic_9"


def _acme_project(path: Path, **changes) -> str:
    """A 300 x 300 x 400 mm printer no catalogue row describes."""
    return _bambu_project(path, **{
        "printer_model": "Acme Printomatic 9",
        "printer_settings_id": "Acme Printomatic 9 0.4 nozzle",
        "printable_area": ["0x0", "300x0", "300x300", "0x300"],
        "printable_height": "400",
        **changes,
    })


class TestAPrinterOutsideTheCatalogue:
    def test_its_slicer_file_sets_it_up_on_this_machine(self, config, tmp_path):
        out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert (out["success"], out["applied"], out["printer_model"]) == (True, True, ACME)
        assert out["in_catalogue"] is False
        assert _saved(config)["shed"]["printer_model"] == ACME
        assert "not in Kiln's catalogue" in out["message"] and "300 x 300 x 400 mm bed" in out["message"]

    def test_the_bed_is_the_files_and_every_limit_is_kilns_generic_one(self, config, tmp_path):
        """Nothing in the file raises a limit: a file stating a 320 C
        nozzle range and 500 mm/s leaves the generic ceilings where they
        were, and the profile says the limits are not this printer's own."""
        project = _acme_project(
            tmp_path / "part.3mf", nozzle_temperature_range_high=["320"], hot_plate_temp=["120"],
            machine_max_speed_x=["500", "500"],
        )
        set_printer_model(slicer_file=project, printer_name="shed", config_path=config)
        generic, saved = sp.get_profile("default"), sp.get_profile(ACME)
        assert saved.build_volume == [300.0, 300.0, 400.0]
        assert (saved.max_hotend_temp, saved.max_bed_temp, saved.max_feedrate) == (
            generic.max_hotend_temp, generic.max_bed_temp, generic.max_feedrate,
        )
        assert saved.curated_base is False

    def test_a_design_is_now_measured_against_its_bed_and_told_whose_number_it_is(self, config, tmp_path):
        """The validation pipeline had nothing to measure against.  It now
        has the bed -- and says it is the owner's, because a bed nobody
        verified can be bigger on paper than in the room."""
        from kiln.plugins._validation_pipeline_internals import _resolve_build_volume
        from kiln.printer_model_resolver import resolve_printer_model_for
        from kiln.printers.bed_fit import check_bed_fit, owner_stated_build_volume

        assert owner_stated_build_volume(ACME) is None
        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        model = resolve_printer_model_for("shed")
        assert model == ACME and owner_stated_build_volume(model) == (300.0, 300.0, 400.0)

        resolved = _resolve_build_volume(model)
        assert resolved.dims == (300.0, 300.0, 400.0)
        assert "owner-set limit, not Kiln-verified" in resolved.provenance

        def box(size: float) -> dict[str, float]:
            return {"x_min": 0.0, "x_max": size, "y_min": 0.0, "y_max": size, "z_min": 0.0, "z_max": 10.0}

        assert check_bed_fit(box(280.0), resolved.dims, source="mesh")["ok"] is True
        assert check_bed_fit(box(320.0), resolved.dims, source="mesh")["error_code"] == "EXCEEDS_BED"

    def test_the_motion_planner_and_the_print_start_bounds_still_read_the_catalogue_alone(self, config, tmp_path):
        """A bed somebody's file stated lays a slice out; it never tells
        Kiln where a head may travel."""
        from kiln.printers.bed_fit import get_build_volume, resolve_build_volume, validate_mesh_for_printer

        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert get_build_volume(ACME) is None and resolve_build_volume(ACME) is None
        assert validate_mesh_for_printer(str(tmp_path / "absent.stl"), ACME)["build_volume"] is None

    def test_a_slice_for_it_is_laid_out_on_its_own_bed(self, config, tmp_path):
        from kiln.slicer_orca import ini_to_settings
        from kiln.slicer_profiles import resolve_slicer_profile

        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        settings = ini_to_settings(resolve_slicer_profile(ACME))
        assert settings["bed_shape"] == "0x0,300x0,300x300,0x300"
        assert settings["max_print_height"] == "400"

    def test_a_slice_for_it_is_for_the_nozzle_on_record_for_that_machine(self, config, tmp_path, monkeypatch):
        """It slices with the generic profile, and is still asked for by its
        own model: the machine set up as it answers for the nozzle."""
        from kiln.slicer_orca import ini_to_settings
        from kiln.slicer_profiles import nozzle_fit_of, resolve_slicer_profile

        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)

        class _Registry:
            def list_machines(self):
                return ["garage", "shed"]

        monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: _Registry())
        monkeypatch.setattr(
            bridge, "consult_recorded_nozzle",
            lambda pid: {"diameter_mm": 0.6 if pid == "shed" else None, "answered": True},
        )
        path = resolve_slicer_profile(ACME)
        assert ini_to_settings(path)["nozzle_diameter"] == "0.6"
        assert nozzle_fit_of(path)["printer_id"] == "shed"

    def test_every_slicing_door_finds_it_by_its_own_key(self, config, tmp_path):
        """The doors turn a printer's model into a profile id through one
        shared mapping, and it answered ``None`` for a printer set up here:
        the slice then ran on the slicer's own bed.  Found by slicing
        through the real tool, not by any test of the profile resolver."""
        from kiln.printer_profile_ids import map_printer_hint_to_profile_id

        assert map_printer_hint_to_profile_id(ACME) is None
        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert map_printer_hint_to_profile_id(ACME) == ACME
        # A key nobody set up keeps the answer it always had.
        assert map_printer_hint_to_profile_id("custom_ender3") == map_printer_hint_to_profile_id("ender3")

    @pytest.mark.skipif(_PRUSASLICER is None, reason="PrusaSlicer not installed")
    def test_the_slicing_tool_lays_a_part_out_on_its_bed(self, config, tmp_path):
        """Through the registered tool, read from the G-code: a 40 mm plate
        lands in the middle of the 300 mm bed, not the middle of the 200 mm
        one the slicer assumes when nobody tells it."""
        import asyncio

        from kiln import server

        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        out = asyncio.run(server.mcp.call_tool("slice_model", {
            "input_path": _plate(tmp_path / "plate.stl"), "output_dir": str(tmp_path / "out"),
            "printer_name": "shed", "slicer_path": _PRUSASLICER, "material": "PLA",
        }))
        reply = json.loads((out[0] if isinstance(out, tuple) else out)[0].text)
        assert reply["success"] is True, reply
        assert reply["printer_id"] == ACME and reply["nozzle"]["diameter_mm"] == 0.4

        gcode = Path(reply["output_path"]).read_text(encoding="utf-8", errors="replace")
        xs = [float(x) for x in re.findall(r"^G1 X(-?\d+\.?\d*) Y-?\d+\.?\d* E", gcode, re.MULTILINE)]
        ys = [float(y) for y in re.findall(r"^G1 X-?\d+\.?\d* Y(-?\d+\.?\d*) E", gcode, re.MULTILINE)]
        assert (min(xs) + max(xs)) / 2 == pytest.approx(150.0, abs=3.0)
        assert (min(ys) + max(ys)) / 2 == pytest.approx(150.0, abs=3.0)

    def test_the_generic_profile_and_a_bundled_one_keep_their_own_bed(self, config, tmp_path):
        from kiln.slicer_orca import ini_to_settings
        from kiln.slicer_profiles import resolve_slicer_profile

        before = ini_to_settings(resolve_slicer_profile("bambu_a1")).get("bed_shape")
        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert "bed_shape" not in ini_to_settings(resolve_slicer_profile("default"))
        assert ini_to_settings(resolve_slicer_profile("bambu_a1")).get("bed_shape") == before

    def test_a_bundled_profiles_bed_is_its_own_whatever_else_kiln_holds(self, monkeypatch):
        from kiln.slicer_orca import ini_to_settings
        from kiln.slicer_profiles import resolve_slicer_profile

        monkeypatch.setattr("kiln.printers.bed_fit.get_build_volume", lambda printer_id: (999.0, 999.0, 999.0))
        assert ini_to_settings(resolve_slicer_profile("bambu_a1"))["bed_shape"] == "0x0,256x0,256x256,0x256"

    def test_it_can_then_be_named_like_any_other(self, config, tmp_path):
        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["printers"]["attic"] = {"type": "octoprint", "host": "http://attic.local"}
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        for said in ("Acme Printomatic 9", ACME):
            out = set_printer_model(said, printer_name="attic", replace=True, config_path=config)
            assert out["success"] and out["printer_model"] == ACME and out["in_catalogue"] is False

    def test_a_setup_already_on_this_machine_is_never_overwritten(self, config, tmp_path):
        """An owner who tightened this printer's limits keeps them: the
        file is a second opinion about the bed, said as a note."""
        sp.set_local_printer_override(ACME, {
            "max_hotend_temp": 220.0, "max_bed_temp": 70.0, "max_feedrate": 6000.0, "build_volume": [280.0, 280.0, 380.0],
        })
        out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert out["applied"] and sp.get_profile(ACME).max_hotend_temp == 220.0
        assert sp.get_profile(ACME).build_volume == [280.0, 280.0, 380.0]
        assert any("300 x 300 x 400 mm" in note and "280 x 280 x 380 mm" in note for note in out["notes"])

    def test_a_refusal_saves_nothing(self, config, tmp_path):
        set_printer_model("Voron 2.4", printer_name="shed", config_path=config)
        out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        assert out["code"] == "MODEL_ALREADY_SET"
        assert sp.list_local_printer_overrides() == []

    def test_with_no_printer_to_set_nothing_is_saved_and_the_next_step_is_said(self, tmp_path):
        out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), config_path=tmp_path / "none.yaml")
        assert (out["success"], out["applied"], out["printer_model"]) == (True, False, None)
        assert "add the printer with register_printer" in out["message"]
        assert sp.list_local_printer_overrides() == []

    def test_a_bambu_connection_may_be_a_model_the_catalogue_has_not_met(self, config, tmp_path):
        out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), config_path=config)
        assert out["applied"] and out["printer"] == "garage"

    def test_a_locked_setup_is_refused_by_name(self, config, tmp_path):
        sp._load_locks()
        sp._locked_profiles.add(ACME)
        try:
            out = set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        finally:
            sp._locked_profiles.discard(ACME)
        assert out["code"] == "LOCAL_SETUP_REFUSED" and "admin-locked" in out["error"]
        assert "printer_model" not in _saved(config)["shed"]

    def test_a_refused_head_move_says_why_not_to_set_a_model_already_set(self, config, tmp_path):
        from types import SimpleNamespace

        from kiln.printers.base import PrinterAdapter

        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        local = PrinterAdapter._declare_model_text(SimpleNamespace(declared_printer_model=lambda: ACME))
        assert "outside Kiln's catalogue" in local and "set-model" not in local
        typo = PrinterAdapter._declare_model_text(SimpleNamespace(declared_printer_model=lambda: "bambu_a11"))
        assert "set-model" in typo

    def test_a_catalogue_printers_bed_is_never_the_one_somebody_typed(self):
        """An override filed under a loose spelling of a catalogue printer
        is a tightened limit, not that printer's bed."""
        from kiln.printers.bed_fit import get_build_volume, owner_stated_build_volume

        catalogue = get_build_volume("Bambu Lab A1")
        sp.set_local_printer_override("bambu_lab_a1", {
            "max_hotend_temp": 200.0, "max_bed_temp": 60.0, "max_feedrate": 6000.0, "build_volume": [100.0, 100.0, 100.0],
        })
        assert get_build_volume("Bambu Lab A1") == catalogue
        for spelling in ("Bambu Lab A1", "bambu_lab_a1", "bambu_a1"):
            assert owner_stated_build_volume(spelling) is None

    def test_the_generic_rows_name_is_not_a_printers(self):
        from kiln.printers.bed_fit import get_build_volume, owner_stated_build_volume

        sp.set_local_printer_override("default", {
            "max_hotend_temp": 200.0, "max_bed_temp": 60.0, "max_feedrate": 6000.0, "build_volume": [100.0, 100.0, 100.0],
        })
        assert get_build_volume("default") is None and owner_stated_build_volume("default") is None

    def test_a_newer_file_corrects_the_bed_only_when_asked(self, config, tmp_path):
        set_printer_model(slicer_file=_acme_project(tmp_path / "old.3mf"), printer_name="shed", config_path=config)
        newer = _acme_project(
            tmp_path / "new.3mf", printable_area=["0x0", "350x0", "350x350", "0x350"], printable_height="400",
        )
        kept = set_printer_model(slicer_file=newer, printer_name="shed", config_path=config)
        assert kept["applied"] is False and sp.get_profile(ACME).build_volume == [300.0, 300.0, 400.0]
        assert any("pass replace=True to take this file's" in note for note in kept["notes"])

        taken = set_printer_model(slicer_file=newer, printer_name="shed", replace=True, config_path=config)
        assert taken["applied"] is True and sp.get_profile(ACME).build_volume == [350.0, 350.0, 400.0]
        assert _saved(config)["shed"]["printer_model"] == ACME
        assert not any("printable volume" in note for note in taken["notes"])

    def test_limits_an_owner_typed_are_not_written_over_even_when_asked(self, config, tmp_path):
        sp.set_local_printer_override(ACME, {
            "max_hotend_temp": 220.0, "max_bed_temp": 70.0, "max_feedrate": 6000.0, "build_volume": [280.0, 280.0, 380.0],
        })
        out = set_printer_model(
            slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", replace=True, config_path=config,
        )
        assert out["success"] and sp.get_profile(ACME).build_volume == [280.0, 280.0, 380.0]
        assert sp.get_profile(ACME).max_hotend_temp == 220.0
        assert not any("replace=True" in note for note in out["notes"])

    def test_a_printer_set_up_here_is_not_swapped_for_another_model_unasked(self, config, tmp_path):
        set_printer_model(slicer_file=_acme_project(tmp_path / "part.3mf"), printer_name="shed", config_path=config)
        out = set_printer_model("Voron 2.4", printer_name="shed", config_path=config)
        assert (out["code"], out["previous"]) == ("MODEL_ALREADY_SET", ACME)
        assert _saved(config)["shed"]["printer_model"] == ACME


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
