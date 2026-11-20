"""Slicing for a printer with more than one nozzle position.

Covered here, with an invented machine and placeholder preset extras: the
printer's nozzle record travels from the resolved profile to the preset
writer; the writer widens its per-extruder values and lays the record's
preset extras over the presets, and writes the same bytes as before when
there is no record or the record is sliced for one nozzle; a caller's
filament-to-nozzle map is checked, handed to the slicer and read back from
the sliced file; and each refusal is said in plain words before or after
the slicer runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln import slicer_profiles
from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
from kiln.slicer import (
    REFUSED_NOZZLE_REACH,
    SOFT_NOZZLE_CONFIRM,
    NozzleMapError,
    SlicerError,
    SlicerInfo,
    SlicerRefusal,
    _orca_refusal,
    checked_filament_map,
    filament_map_of,
    slice_file,
)
from kiln.slicer_orca import PresetExtrasError, settings_to_orca_presets, write_orca_presets
from kiln.slicer_profiles import (
    ProgramSettingError,
    nozzle_positions,
    nozzles_of,
    profile_with_overrides,
    resolve_slicer_profile,
)

_SETTINGS = {
    "nozzle_diameter": "0.4",
    "retract_length": "0.8",
    "layer_height": "0.2",
    "bed_shape": "0x0,200x0,200x200,0x200",
    "temperature": "220",
    "bed_temperature": "60",
    "use_relative_e_distances": "1",
    "gcode_flavor": "marlin",
}

#: An invented machine's nozzle record.  The extras are placeholders: the
#: writer lays over whatever the record carries.
_NOZZLES = {
    "kind": "fixed_pair",
    "sliced_as": 2,
    "maker_slicer": {"name": "bambu_studio"},
    "preset_extras": {
        "machine": {"example_machine_key": ["a", "b"], "gcode_flavor": "klipper"},
        "process": {"example_process_key": "p"},
        "filament": {"example_filament_key": ["f"]},
    },
}

_COLORS = ["#FFFFFF", "#000000"]


def _studio() -> SlicerInfo:
    return SlicerInfo(path="/fake/BambuStudio", name="bambustudio")


def _orca() -> SlicerInfo:
    return SlicerInfo(path="/fake/OrcaSlicer", name="orcaslicer", version="OrcaSlicer-2.3.2:")


def _prusa() -> SlicerInfo:
    return SlicerInfo(path="/fake/PrusaSlicer", name="prusa-slicer", version="PrusaSlicer-2.9.4 based on Slic3r")


def _cube(path: Path, x: float) -> str:
    lo, hi, y = x, x + 20.0, 90.0
    v = [
        (lo, y, 0), (hi, y, 0), (hi, y + 20, 0), (lo, y + 20, 0),
        (lo, y, 10), (hi, y, 10), (hi, y + 20, 10), (lo, y + 20, 10),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _plate(tmp_path: Path, filaments: int = 2) -> str:
    """One 20 mm cube per filament, side by side, as a 3MF."""
    parts = [
        ColorPart(_cube(tmp_path / f"part{i}.stl", 40.0 + 60.0 * i), extruder=i + 1, name=f"part{i}")
        for i in range(filaments)
    ]
    composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / "plate.3mf"))
    assert composed["success"], composed
    return composed["output_path"]


def _profile(tmp_path: Path, nozzles: dict | None = _NOZZLES) -> str:
    """A profile for the invented machine, carrying *nozzles* when given."""
    ini = tmp_path / "invented.ini"
    body = "".join(f"{k} = {v}\n" for k, v in _SETTINGS.items())
    ini.write_text(slicer_profiles._with_nozzles(body, nozzles))
    return str(ini)


#: A sliced body that homes first and changes filament inside the change routine.
_STARTED_BODY = "G28\nG1 X10 Y10 E1\nM620 S1A\nT1\nM621 S1A\nG1 X20 Y20 E1\n"


def _slicer_that_writes(header: str, seen: dict, returncode: int = 0, said: str = "", body: str = "T0\nT1\n"):
    """A stand-in for the slicer run: records the command line, the timeout
    and the presets it was handed, and writes *header* then *body* as the
    sliced file; asked for a print package, it writes one holding that file."""

    def run(cmd, *, timeout, retryable_returncodes, remove_partial_output, output_is_complete, cwd=None):
        seen["cmd"], seen["timeout"] = list(cmd), timeout
        machine, process = cmd[cmd.index("--load-settings") + 1].split(";")
        seen["machine"] = json.loads(Path(machine).read_text())
        seen["process"] = json.loads(Path(process).read_text())
        seen["filaments"] = [
            json.loads(Path(f).read_text()) for f in cmd[cmd.index("--load-filaments") + 1].split(";")
        ]
        work_dir = cmd[cmd.index("--outputdir") + 1]
        if returncode == 0:
            gcode = f"{header}{body}; filament used [mm] = 1.0, 1.0\n"
            Path(work_dir, "plate_1.gcode").write_text(gcode)
            if "--export-3mf" in cmd:
                _write_package(Path(work_dir, cmd[cmd.index("--export-3mf") + 1]), gcode.encode())
        else:
            Path(work_dir, "result.json").write_text(json.dumps({"error_string": said}))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    return run


def _write_package(path: Path, gcode: bytes, *, md5: str | None = None) -> None:
    """A print package holding *gcode* as its plate, with its checksum."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("Metadata/plate_1.gcode.md5", md5 or hashlib.md5(gcode).hexdigest().upper())


def _never_runs(*_args, **_kwargs):
    raise AssertionError("the slicer was launched")


# ---------------------------------------------------------------------------
# The preset writer
# ---------------------------------------------------------------------------


class TestThePresetWriter:
    def test_a_record_for_two_positions_widens_the_per_extruder_values(self):
        presets = settings_to_orca_presets(_SETTINGS, nozzles=_NOZZLES)
        assert presets.machine["nozzle_diameter"] == ["0.4", "0.4"]
        assert presets.machine["retraction_length"] == ["0.8", "0.8"]

    def test_three_positions_are_three_entries(self):
        presets = settings_to_orca_presets(_SETTINGS, nozzles={"sliced_as": 3})
        assert presets.machine["nozzle_diameter"] == ["0.4", "0.4", "0.4"]

    def test_the_extras_are_laid_over_each_preset_and_win(self):
        presets = settings_to_orca_presets(_SETTINGS, nozzles=_NOZZLES)
        assert presets.machine["example_machine_key"] == ["a", "b"]
        # A key the writer already wrote from the settings is replaced.
        assert presets.machine["gcode_flavor"] == "klipper"
        assert presets.process["example_process_key"] == "p"
        assert presets.filament["example_filament_key"] == ["f"]

    def test_an_extra_wins_over_a_widened_value(self):
        nozzles = {"sliced_as": 2, "preset_extras": {"machine": {"nozzle_diameter": ["0.4", "0.6"]}}}
        assert settings_to_orca_presets(_SETTINGS, nozzles=nozzles).machine["nozzle_diameter"] == ["0.4", "0.6"]

    def test_the_filament_extras_reach_every_filament_preset(self):
        presets = settings_to_orca_presets(_SETTINGS, filament_colors=_COLORS, nozzles=_NOZZLES)
        assert [f["example_filament_key"] for f in presets.filaments] == [["f"], ["f"]]
        assert [f["filament_colour"] for f in presets.filaments] == [["#FFFFFF"], ["#000000"]]

    @pytest.mark.parametrize("nozzles", [None, {}, {"sliced_as": 1, "preset_extras": _NOZZLES["preset_extras"]}])
    @pytest.mark.parametrize("colors", [None, _COLORS])
    def test_no_record_or_one_position_writes_the_same_bytes(self, tmp_path, nozzles, colors):
        before, after = tmp_path / "before", tmp_path / "after"
        write_orca_presets(_SETTINGS, str(before), filament_colors=colors, wipe_tower_xy=(10.0, 10.0))
        write_orca_presets(
            _SETTINGS, str(after), filament_colors=colors, wipe_tower_xy=(10.0, 10.0), nozzles=nozzles,
        )
        names = sorted(p.name for p in before.iterdir())
        assert names == sorted(p.name for p in after.iterdir()) and len(names) == (4 if colors else 3)
        for name in names:
            assert (before / name).read_bytes() == (after / name).read_bytes(), name

    def test_one_position_leaves_the_extras_out(self):
        presets = settings_to_orca_presets(_SETTINGS, nozzles={**_NOZZLES, "sliced_as": 1})
        assert "example_machine_key" not in presets.machine
        assert presets.machine["nozzle_diameter"] == ["0.4"]

    def test_an_extra_that_runs_a_program_is_refused(self):
        nozzles = {"sliced_as": 2, "preset_extras": {"process": {"post_process": ["/bin/anything"]}}}
        with pytest.raises(ProgramSettingError):
            settings_to_orca_presets(_SETTINGS, nozzles=nozzles)

    @pytest.mark.parametrize("key", ["nozzle_temperature", "hot_plate_temp", "chamber_temperatures"])
    def test_an_extra_that_commands_a_heater_is_refused(self, key):
        nozzles = {"sliced_as": 2, "preset_extras": {"filament": {key: ["400"]}}}
        with pytest.raises(PresetExtrasError, match="temperature"):
            settings_to_orca_presets(_SETTINGS, nozzles=nozzles)

    @pytest.mark.parametrize(
        "extras",
        [
            {"printer": {"example": "x"}},
            {"machine": ["example"]},
            {"machine": {"example": 4}},
            {"machine": {"example": ["a", 2]}},
            "machine",
        ],
    )
    def test_extras_of_another_shape_are_refused(self, extras):
        with pytest.raises(PresetExtrasError):
            settings_to_orca_presets(_SETTINGS, nozzles={"sliced_as": 2, "preset_extras": extras})

    @pytest.mark.parametrize("raw, positions", [(2, 2), (4, 4), (1, 1), (0, 1), ("2", 1), (True, 1), (None, 1)])
    def test_the_positions_a_record_is_sliced_for(self, raw, positions):
        assert nozzle_positions({"sliced_as": raw}) == positions


# ---------------------------------------------------------------------------
# The record travels with the profile
# ---------------------------------------------------------------------------


@pytest.fixture
def invented_printer(monkeypatch):
    """A held profile for an invented machine; returns a setter for its
    nozzle record."""
    slicer_profiles._load()
    row = {"display_name": "Invented Two", "slicer": "prusaslicer", "notes": "", "settings": dict(_SETTINGS)}
    monkeypatch.setitem(slicer_profiles._cache, "invented_two", slicer_profiles._parse_profile("invented_two", row))
    monkeypatch.setitem(slicer_profiles._raw_rows, "invented_two", row)
    monkeypatch.setattr(slicer_profiles, "_temp_cache", {})

    def set_nozzles(block):
        row.pop("nozzles", None)
        if block is not None:
            row["nozzles"] = block

    return set_nozzles


class TestTheRecordTravelsWithTheProfile:
    def test_the_resolved_profile_carries_the_record(self, invented_printer):
        invented_printer(_NOZZLES)
        assert nozzles_of(resolve_slicer_profile("invented_two")) == _NOZZLES

    def test_a_profile_derived_from_it_still_carries_the_record(self, invented_printer):
        invented_printer(_NOZZLES)
        derived = profile_with_overrides(resolve_slicer_profile("invented_two"), {"layer_height": "0.12"})
        assert nozzles_of(derived) == _NOZZLES

    def test_the_several_extruder_profile_carries_it_too(self, invented_printer):
        invented_printer(_NOZZLES)
        assert nozzles_of(slicer_profiles.resolve_multiextruder_profile("invented_two", 2)) == _NOZZLES

    @pytest.mark.parametrize("block", [{"kind": "single", "sliced_as": 1, "preset_extras": {}}, {"stock": 1, "max": 4}])
    def test_a_record_for_one_position_leaves_the_profile_as_it_was(self, invented_printer, block):
        invented_printer(None)
        plain = Path(resolve_slicer_profile("invented_two")).read_bytes()
        invented_printer(block)
        path = resolve_slicer_profile("invented_two")
        assert Path(path).read_bytes() == plain
        assert nozzles_of(path) is None

    def test_the_settings_are_the_same_with_and_without_the_record(self, invented_printer):
        from kiln.slicer_orca import ini_to_settings

        invented_printer(None)
        plain = ini_to_settings(resolve_slicer_profile("invented_two"))
        invented_printer(_NOZZLES)
        assert ini_to_settings(resolve_slicer_profile("invented_two")) == plain

    def test_a_profile_with_no_record_has_none(self, tmp_path):
        assert nozzles_of(_profile(tmp_path, None)) is None
        assert nozzles_of(None) is None
        assert nozzles_of(str(tmp_path / "missing.ini")) is None


# ---------------------------------------------------------------------------
# The filament-to-nozzle map
# ---------------------------------------------------------------------------


class TestTheMapIsChecked:
    def test_a_map_that_fits_is_the_list_asked_for(self):
        assert checked_filament_map([2, 1], filaments=2, positions=2) == [2, 1]
        assert checked_filament_map("2, 1", filaments=2, positions=2) == [2, 1]

    @pytest.mark.parametrize("nothing", [None, "", []])
    def test_no_map_is_none(self, nothing):
        assert checked_filament_map(nothing, filaments=2, positions=2) is None
        assert checked_filament_map(nothing, filaments=1, positions=1) is None

    @pytest.mark.parametrize("asked", [[1], [1, 2, 1]])
    def test_the_wrong_length_is_refused(self, asked):
        with pytest.raises(NozzleMapError) as refused:
            checked_filament_map(asked, filaments=2, positions=2)
        assert refused.value.code == "NOZZLE_MAP_WRONG_LENGTH"
        assert "one nozzle number for each filament" in str(refused.value)

    @pytest.mark.parametrize("asked", [[1, 3], [0, 1], [1, -1], [1, "left"], [1, 1.5], [True, 1]])
    def test_a_nozzle_the_printer_does_not_have_is_refused(self, asked):
        with pytest.raises(NozzleMapError) as refused:
            checked_filament_map(asked, filaments=2, positions=2)
        assert refused.value.code == "NO_SUCH_NOZZLE"
        assert "nozzles 1 to 2" in str(refused.value)

    def test_a_map_for_a_printer_sliced_with_one_nozzle_is_refused(self):
        with pytest.raises(NozzleMapError) as refused:
            checked_filament_map([1, 1], filaments=2, positions=1)
        assert refused.value.code == "NOZZLE_MAP_NOT_USED"


class TestTheMapIsReadBack:
    def test_the_map_and_who_chose_it(self, tmp_path):
        gcode = tmp_path / "a.gcode"
        gcode.write_text("; filament_map_mode = Manual\n; filament_map = 2,1,2\nG1 X1\n")
        assert filament_map_of(str(gcode)) == {
            "filament_to_nozzle": [2, 1, 2],
            "nozzle_filaments": {"2": [1, 3], "1": [2]},
            "mode": "Manual",
        }

    def test_a_file_that_states_no_map_has_none(self, tmp_path):
        gcode = tmp_path / "a.gcode"
        gcode.write_text("; filament_map_mode = Manual\n; filament_mapping = 1,2\nG1 X1\n")
        assert filament_map_of(str(gcode)) is None
        assert filament_map_of(str(tmp_path / "missing.gcode")) is None
        assert filament_map_of(None) is None

    def test_a_map_that_is_not_numbers_is_not_read(self, tmp_path):
        gcode = tmp_path / "a.gcode"
        gcode.write_text("; filament_map = left,right\n")
        assert filament_map_of(str(gcode)) is None


# ---------------------------------------------------------------------------
# The slice
# ---------------------------------------------------------------------------


class TestTheSlice:
    def _slice(self, tmp_path, run, *, slicer=None, studio=None, filaments=2, nozzles=_NOZZLES, **kwargs):
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer or _studio()),
            patch("kiln.maker_slicer._find", return_value=studio),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, filaments), output_dir=str(tmp_path / "out"),
                profile=_profile(tmp_path, nozzles), **kwargs,
            )

    def test_the_map_asked_for_is_handed_to_the_slicer_and_read_back(self, tmp_path):
        seen: dict = {}
        header = "; filament_map = 2,1\n; filament_map_mode = Manual\n"
        result = self._slice(tmp_path, _slicer_that_writes(header, seen), filament_map=[2, 1], timeout=77)
        cmd = seen["cmd"]
        at = cmd.index("--filament-map-mode")
        assert cmd[at:at + 4] == ["--filament-map-mode", "Manual", "--filament-map", "2,1"]
        assert seen["timeout"] == 77
        assert result.filament_map == {
            "filament_to_nozzle": [2, 1], "nozzle_filaments": {"2": [1], "1": [2]}, "mode": "Manual",
        }
        assert result.to_dict()["filament_map"]["filament_to_nozzle"] == [2, 1]

    def test_the_presets_handed_over_are_for_both_positions(self, tmp_path):
        seen: dict = {}
        self._slice(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen))
        assert seen["machine"]["nozzle_diameter"] == ["0.4", "0.4"]
        assert seen["machine"]["example_machine_key"] == ["a", "b"]
        assert seen["process"]["example_process_key"] == "p"
        assert [f["example_filament_key"] for f in seen["filaments"]] == [["f"], ["f"]]

    def test_with_no_map_the_slicer_chooses_and_its_choice_is_reported(self, tmp_path):
        seen: dict = {}
        header = "; filament_map = 1,2\n; filament_map_mode = Auto For Flush\n"
        result = self._slice(tmp_path, _slicer_that_writes(header, seen))
        assert not any(arg.startswith("--filament-map") for arg in seen["cmd"])
        assert result.filament_map["filament_to_nozzle"] == [1, 2]
        assert result.filament_map["mode"] == "Auto For Flush"

    @pytest.mark.parametrize("header", ["; filament_map = 1,2\n", ""])
    def test_a_file_sliced_with_another_map_is_not_kept(self, tmp_path, header):
        with pytest.raises(SlicerError, match="did not slice with the filament-to-nozzle map"):
            self._slice(tmp_path, _slicer_that_writes(header, {}), filament_map=[2, 1])
        assert list((tmp_path / "out").glob("*.gcode")) == []

    @pytest.mark.parametrize(
        "asked, code",
        [([1], "NOZZLE_MAP_WRONG_LENGTH"), ([1, 2, 1], "NOZZLE_MAP_WRONG_LENGTH"), ([1, 3], "NO_SUCH_NOZZLE")],
    )
    def test_a_map_that_does_not_fit_is_refused_before_slicing(self, tmp_path, asked, code):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, filament_map=asked)
        assert refused.value.code == code

    def test_a_map_for_a_one_nozzle_printer_is_refused_before_slicing(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, nozzles=None, filament_map=[1, 1])
        assert refused.value.code == "NOZZLE_MAP_NOT_USED"

    def test_without_bambu_studio_the_print_is_sliced_for_one_nozzle(self, tmp_path, monkeypatch):
        from kiln import maker_slicer, maker_slicer_notes

        monkeypatch.setattr(maker_slicer_notes, "store_path", lambda: tmp_path / "notes.json")
        monkeypatch.setattr(maker_slicer, "_brew_command", lambda *_a, **_k: None)
        with_record, without = {}, {}
        result = self._slice(tmp_path, _slicer_that_writes("", with_record), slicer=_orca())
        self._slice(tmp_path, _slicer_that_writes("", without), slicer=_orca(), nozzles=None)
        # The presets handed over are the ones a printer sliced with one nozzle gets.
        named = {"name", "printer_model", "compatible_printers"}

        def body(preset: dict) -> dict:
            return {key: value for key, value in preset.items() if key not in named}

        assert body(with_record["machine"]) == body(without["machine"])
        assert not any(arg.startswith("--filament-map") for arg in with_record["cmd"])
        assert result.filament_map is None and result.sliced_with is None
        assert result.one_nozzle["filament_swaps"] == 1
        assert result.one_nozzle["maker_slicer"] == {
            "name": "Bambu Studio", "need": "install", "download": "bambulab.com/download/studio",
        }
        assert "Sliced for one nozzle: 1 filament swap." in result.message
        assert "Install Bambu Studio (free) and Kiln uses both nozzles on its own" in result.message

    def test_without_bambu_studio_a_map_is_refused_before_launching(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, slicer=_prusa(), filament_map=[2, 1])
        assert refused.value.code == "MULTI_NOZZLE_NEEDS_MAKER_SLICER"
        said = str(refused.value)
        assert "Bambu Studio" in said and "install" in said and "nothing was sliced" in said
        assert "leave the map out" in said

    def test_an_explicit_slicer_is_not_swapped_for_another(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, slicer=_orca(), studio=_studio(), slicer_path="/fake/OrcaSlicer")
        assert refused.value.code == "MULTI_NOZZLE_NEEDS_MAKER_SLICER"

    @pytest.mark.parametrize("slicer", [_prusa(), _orca()])
    def test_an_installed_bambu_studio_is_used_instead(self, tmp_path, slicer):
        seen: dict = {}
        result = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), slicer=slicer, studio=_studio(),
        )
        assert seen["cmd"][0] == "/fake/BambuStudio"
        assert result.filament_map["filament_to_nozzle"] == [1, 2]

    def test_one_filament_and_no_map_slices_for_one_nozzle(self, tmp_path):
        with_record, without = {}, {}
        result = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1\n", with_record), slicer=_orca(), filaments=1,
        )
        self._slice(tmp_path, _slicer_that_writes("", without), slicer=_orca(), filaments=1, nozzles=None)
        assert result.filament_map is None
        # Each run names its presets after its own profile file.
        named = {"name", "printer_model", "compatible_printers"}

        def body(preset: dict) -> dict:
            return {key: value for key, value in preset.items() if key not in named}

        assert body(with_record["machine"]) == body(without["machine"])
        assert body(with_record["process"]) == body(without["process"])
        assert [body(f) for f in with_record["filaments"]] == [body(f) for f in without["filaments"]]
        assert with_record["machine"]["nozzle_diameter"] == ["0.4"]

    def test_one_filament_with_a_map_slices_for_the_nozzle_named(self, tmp_path):
        seen: dict = {}
        result = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 2\n", seen), filaments=1, filament_map=[2],
        )
        assert seen["cmd"][seen["cmd"].index("--filament-map") + 1] == "2"
        assert seen["machine"]["nozzle_diameter"] == ["0.4", "0.4"]
        assert result.filament_map["nozzle_filaments"] == {"2": [1]}

    def test_a_filament_on_a_nozzle_that_cannot_reach_is_refused_in_plain_words(self, tmp_path):
        said = "Some filaments cannot be mapped to correct extruders for multi-extruder Printer."
        with pytest.raises(SlicerRefusal) as refused:
            self._slice(tmp_path, _slicer_that_writes("", {}, returncode=190, said=said), filament_map=[2, 1])
        assert refused.value.kind == REFUSED_NOZZLE_REACH
        assert refused.value.code == "NOZZLE_CANNOT_REACH"
        assert (
            "that nozzle cannot reach this part; move it toward the middle or use the other nozzle"
            in str(refused.value)
        )
        assert not re.search(r"\d", str(refused.value))
        assert list((tmp_path / "out").glob("*.gcode")) == []

    @pytest.mark.parametrize("status, said", [(190, ""), (1, "Some filaments cannot be mapped to correct extruders")])
    def test_the_refusal_is_known_by_its_status_or_its_words(self, status, said):
        refusal = _orca_refusal("BambuStudio", status, said)
        assert refusal is not None and refusal.kind == REFUSED_NOZZLE_REACH


# ---------------------------------------------------------------------------
# The slice_model door
# ---------------------------------------------------------------------------


def _slice_model_tool():
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools["slice_model"]


class TestTheSliceModelDoor:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        from kiln import monitor_twin

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        twin = tmp_path / "twin"
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")

    def _call(self, tmp_path, run, **kwargs):
        with (
            patch("kiln.server._check_auth", return_value=None),
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return _slice_model_tool()(
                input_path=_plate(tmp_path), output_dir=str(tmp_path / "out"), printer_id=None,
                profile=_profile(tmp_path), **kwargs,
            )

    def test_the_map_reaches_the_slicer_and_the_reply_says_what_was_used(self, tmp_path):
        seen: dict = {}
        reply = self._call(
            tmp_path, _slicer_that_writes("; filament_map = 2,1\n; filament_map_mode = Manual\n", seen),
            filament_map=[2, 1],
        )
        assert reply["success"], reply
        assert seen["cmd"][seen["cmd"].index("--filament-map") + 1] == "2,1"
        assert reply["filament_map"]["filament_to_nozzle"] == [2, 1]
        assert "output_3mf_path" not in reply
        assert any("more than one nozzle" in w and "not ready to upload" in w for w in reply["warnings"])

    def test_a_map_that_does_not_fit_is_the_doors_refusal(self, tmp_path):
        reply = self._call(tmp_path, _never_runs, filament_map=[1, 2, 2])
        assert reply["success"] is False
        assert reply["error"]["code"] == "NOZZLE_MAP_WRONG_LENGTH"
        assert "one nozzle number for each filament" in reply["error"]["message"]


# ---------------------------------------------------------------------------
# The print package a slice for more than one nozzle travels in
# ---------------------------------------------------------------------------


def _bambu_3mf():
    from kiln.printers import bambu_3mf

    return bambu_3mf


class TestTheSlicerWritesThePrintPackage:
    def _slice(self, tmp_path, run, **kwargs):
        return TestTheSlice()._slice(tmp_path, run, **kwargs)

    def test_bambu_studio_is_asked_for_it_and_it_is_kept_holding_the_slice(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), filament_map=[1, 2])
        assert "--export-3mf" in seen["cmd"]
        package = _bambu_3mf().print_package_path_for(result.output_path)
        gcode = Path(result.output_path).read_bytes()
        with zipfile.ZipFile(package) as zf:
            # The file as Kiln handed it back, its lineage line included.
            assert zf.read("Metadata/plate_1.gcode") == gcode
            assert zf.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(gcode).hexdigest().upper()
            assert "3D/3dmodel.model" in zf.namelist()
        assert not list((tmp_path / "out").glob("*.made.3mf"))

    def test_a_slice_for_one_nozzle_asks_for_no_package(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes("", seen), filaments=1)
        assert "--export-3mf" not in seen["cmd"]
        assert not Path(_bambu_3mf().print_package_path_for(result.output_path)).exists()

    def test_a_slicer_that_writes_none_still_slices(self, tmp_path):
        def run(cmd, **kwargs):
            reply = _slicer_that_writes("; filament_map = 1,2\n", {})(cmd, **kwargs)
            Path(cmd[cmd.index("--outputdir") + 1], cmd[cmd.index("--export-3mf") + 1]).unlink()
            return reply

        result = self._slice(tmp_path, run, filament_map=[1, 2])
        assert result.success
        assert not Path(_bambu_3mf().print_package_path_for(result.output_path)).exists()


def _sliced(tmp_path: Path, body: str = _STARTED_BODY, header: str = "; filament_map = 1,2\n") -> Path:
    gcode = tmp_path / "plate.gcode"
    gcode.write_text(f"{header}{body}")
    return gcode


class TestThePackageIsJudged:
    def test_a_package_that_homes_and_changes_inside_the_routine_passes(self, tmp_path):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path)
        _write_package(Path(b.print_package_path_for(gcode)), gcode.read_bytes())
        assert b.print_package_problems(b.print_package_path_for(gcode), str(gcode)) == []
        assert b.multi_nozzle_print_package(str(gcode)) == b.print_package_path_for(gcode)

    @pytest.mark.parametrize(
        "body, problem",
        [
            ("G1 X10 Y10 E1\nG28\n", "without homing"),
            ("G28\nG1 X10 Y10 E1\nT1\nG1 X20 Y20 E1\n", "outside the printer's filament-change routine"),
            ("G28\nM620 S1A\nT1\nM621 S1A\nG1 X20 Y20 E1\nT0 H-1\n", "outside the printer's filament-change routine"),
        ],
    )
    def test_a_package_that_cannot_start_is_refused_with_why(self, tmp_path, body, problem):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path, body)
        _write_package(Path(b.print_package_path_for(gcode)), gcode.read_bytes())
        assert any(problem in p for p in b.print_package_problems(b.print_package_path_for(gcode), str(gcode)))
        with pytest.raises(b.MultiNozzlePackageError) as refused:
            b.multi_nozzle_print_package(str(gcode))
        assert refused.value.code == "MULTI_NOZZLE_NOT_PACKAGED"
        assert "not ready to upload or start" in str(refused.value) and problem in str(refused.value)

    def test_a_package_of_another_slice_or_a_wrong_checksum_is_refused(self, tmp_path):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path)
        package = Path(b.print_package_path_for(gcode))
        _write_package(package, gcode.read_bytes(), md5="0" * 32)
        assert b.print_package_problems(str(package), str(gcode)) == ["the package's checksum does not match its plate"]
        _write_package(package, gcode.read_bytes() + b"; another\n")
        assert b.print_package_problems(str(package), str(gcode)) == [
            "the slicer's package holds a different slice from this file"
        ]
        with pytest.raises(b.MultiNozzlePackageError, match="no print package was made with this slice"):
            b.multi_nozzle_print_package(str(gcode))

    def test_a_slice_for_one_nozzle_is_left_to_kilns_own_package(self, tmp_path):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path, header="; filament_map = 1,1\n")
        # Another wrap of a one-nozzle slice at the same name is not a record.
        _write_package(Path(b.print_package_path_for(gcode)), b"G28\n")
        assert b.multi_nozzle_print_package(str(gcode)) is None

    def test_a_slice_for_one_nozzle_with_no_package_is_told_by_its_own_figures(self, tmp_path, monkeypatch):
        # Every Bambu print is asked whether it is one of these: a file that
        # states one nozzle and has no package beside it is answered from
        # the lines a slicer writes its figures in, never by reading it whole.
        b = _bambu_3mf()
        gcode = _sliced(tmp_path, body="G28\n" + "G1 X10 Y10 E0.1\n" * 3000, header="; filament_map = 1,1\n")

        def whole(self, *a, **k):
            raise AssertionError(f"read whole: {self}")

        monkeypatch.setattr(Path, "read_bytes", whole)
        monkeypatch.setattr(Path, "read_text", whole)
        assert b.multi_nozzle_print_package(str(gcode)) is None

    @pytest.mark.parametrize("asked", [{"lift_floor_mm": 12.0}, {"resume_mode": True}])
    def test_a_start_of_kilns_own_is_refused(self, tmp_path, asked):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path)
        _write_package(Path(b.print_package_path_for(gcode)), gcode.read_bytes())
        with pytest.raises(b.MultiNozzlePackageError, match="start of Kiln's own"):
            b.multi_nozzle_print_package(str(gcode), **asked)

    def test_kilns_own_package_builder_refuses_such_a_file(self, tmp_path):
        b = _bambu_3mf()
        with pytest.raises(b.MultiNozzlePackageError, match="sliced for one nozzle only"):
            b.build_bambu_3mf("; filament_map = 2,1\n;LAYER_CHANGE\nG1 X1 Y1 E1\n", str(tmp_path / "x.3mf"))


class TestEveryDoorSendsThePackage:
    def _ready(self, tmp_path, body=_STARTED_BODY):
        b = _bambu_3mf()
        gcode = _sliced(tmp_path, body)
        _write_package(Path(b.print_package_path_for(gcode)), gcode.read_bytes())
        return str(gcode), b.print_package_path_for(gcode)

    def test_the_slice_doors_wrap_helper(self, tmp_path):
        from kiln.plugins.slicer_tools import _auto_wrap_bambu_3mf

        gcode, package = self._ready(tmp_path)
        assert _auto_wrap_bambu_3mf(gcode, "bambu_invented", None) == (package, None)
        assert _auto_wrap_bambu_3mf(gcode, None, None, several_nozzles=True) == (package, None)
        assert _auto_wrap_bambu_3mf(gcode, None, None) == (None, None)

    def test_the_slice_doors_wrap_helper_says_why_not(self, tmp_path):
        from kiln.plugins.slicer_tools import _auto_wrap_bambu_3mf

        gcode, _package = self._ready(tmp_path, body="G1 X1 Y1 E1\n")
        path, warning = _auto_wrap_bambu_3mf(gcode, "bambu_invented", None)
        assert path is None and warning.startswith("This file was sliced for more than one nozzle")
        assert "Bambu auto-wrap failed" not in warning

    def test_the_printers_own_wrap_and_the_upload_step(self, tmp_path):
        from kiln.printers.bambu import BambuAdapter
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        gcode, package = self._ready(tmp_path)
        fake = SimpleNamespace(_printer_model="bambu_invented", active_filament_color=lambda: None)
        fake.wrap_gcode_as_3mf = lambda path, **kw: BambuAdapter.wrap_gcode_as_3mf(fake, path, **kw)
        assert prepare_upload_for_adapter(fake, gcode) == (package, True)

    def test_the_upload_step_never_falls_back_to_the_raw_file(self, tmp_path):
        from kiln.printers.bambu import BambuAdapter
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        gcode, _package = self._ready(tmp_path, body="G1 X1 Y1 E1\n")
        fake = SimpleNamespace(_printer_model="bambu_invented", active_filament_color=lambda: None)
        fake.wrap_gcode_as_3mf = lambda path, **kw: BambuAdapter.wrap_gcode_as_3mf(fake, path, **kw)
        with pytest.raises(_bambu_3mf().MultiNozzlePackageError):
            prepare_upload_for_adapter(fake, gcode)


class TestTheSliceModelDoorSendsThePackage(TestTheSliceModelDoor):
    def test_a_package_that_can_start_is_the_file_to_upload(self, tmp_path):
        with patch("kiln.printers.bed_fit.verify_3mf_is_safe_to_print", return_value={"ok": True}):
            reply = self._call(
                tmp_path, _slicer_that_writes("; filament_map = 2,1\n", {}, body=_STARTED_BODY),
                filament_map=[2, 1],
            )
        assert reply["success"], reply
        assert reply["output_3mf_path"].endswith(".gcode.3mf")
        assert reply["recommended_upload_path"] == reply["output_3mf_path"]
        assert not any("more than one nozzle" in w for w in reply.get("warnings", []))


# ---------------------------------------------------------------------------
# The bed a plate for more than one nozzle is laid out on
# ---------------------------------------------------------------------------

#: The invented machine's record with a wider bed for its two nozzles and
#: where each of them reaches (invented numbers).
_WIDE = {
    **_NOZZLES,
    "bed": "0x0,260x0,260x200,0x200",
    "positions": [{"id": 1, "reach": "0x0,230x0,230x200,0x200"}, {"id": 2, "reach": "30x0,260x0,260x200,30x200"}],
}


def _plate_at(tmp_path: Path, *xs: float) -> str:
    """One 20 mm cube per filament, its left edge at each of *xs*, as a 3MF."""
    name = "_".join(f"{x:g}" for x in xs)
    parts = [
        ColorPart(_cube(tmp_path / f"{name}_{i}.stl", x), extruder=i + 1, name=f"part{i}") for i, x in enumerate(xs)
    ]
    composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / f"plate_{name}.3mf"))
    assert composed["success"], composed
    return composed["output_path"]


class TestTheBedForMoreThanOneNozzle:
    def test_the_records_bed_and_reaches_are_read(self):
        assert slicer_profiles.nozzle_bed(_WIDE) == (0.0, 0.0, 260.0, 200.0)
        assert slicer_profiles.nozzle_reaches(_WIDE) == [(0.0, 0.0, 230.0, 200.0), (30.0, 0.0, 260.0, 200.0)]
        assert slicer_profiles.shared_reach(_WIDE) == (30.0, 0.0, 230.0, 200.0)
        assert slicer_profiles.rect_polygon((30.0, 0.0, 230.0, 200.0)) == "30x0,230x0,230x200,30x200"

    @pytest.mark.parametrize("block", [_NOZZLES, {**_WIDE, "sliced_as": 1}, {**_WIDE, "bed": "wide"}])
    def test_a_record_with_no_readable_bed_states_none(self, block):
        assert slicer_profiles.nozzle_bed(block) is None

    def _slice(self, tmp_path, plate, nozzles=_WIDE, slicer=None, **kwargs):
        seen: dict = {}
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer or _studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _slicer_that_writes("; filament_map = 1,2\n", seen)),
        ):
            slice_file(plate, output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, nozzles), **kwargs)
        return seen

    def test_a_part_only_the_second_nozzle_reaches_is_sliced_on_the_wider_bed(self, tmp_path):
        seen = self._slice(tmp_path, _plate_at(tmp_path, 100.0, 235.0))
        assert seen["machine"]["printable_area"] == ["0x0", "260x0", "260x200", "0x200"]
        # Every nozzle prints the tower, so it stands where both reach.
        assert 30.0 <= float(seen["process"]["wipe_tower_x"]) <= 230.0

    def test_without_a_bed_in_the_record_the_same_plate_is_off_the_bed(self, tmp_path):
        with pytest.raises(SlicerError, match="bed"):
            self._slice(tmp_path, _plate_at(tmp_path, 100.0, 235.0), nozzles=_NOZZLES)

    def test_a_plate_of_one_filament_keeps_the_printers_own_bed(self, tmp_path):
        with pytest.raises(SlicerError, match="bed"):
            self._slice(tmp_path, _plate_at(tmp_path, 235.0))

    def test_the_slice_decides_its_bed_the_same_way_every_door_asks(self, tmp_path):
        from kiln.slicer import nozzle_bed_for_slice

        profile = _profile(tmp_path, _WIDE)
        with patch("kiln.maker_slicer._find", return_value=_studio()):
            assert nozzle_bed_for_slice(_plate_at(tmp_path, 100.0, 235.0), profile) == (0.0, 0.0, 260.0, 200.0)
            one = _plate_at(tmp_path, 40.0)
            assert nozzle_bed_for_slice(one, profile) is None
            assert nozzle_bed_for_slice(one, profile, [2]) == (0.0, 0.0, 260.0, 200.0)
            assert nozzle_bed_for_slice(one, _profile(tmp_path, _NOZZLES), [2]) is None

    def test_without_the_makers_slicer_a_plate_is_held_to_the_printers_own_bed(self, tmp_path):
        from kiln.slicer import nozzle_bed_for_slice

        plate = _plate_at(tmp_path, 100.0, 235.0)
        with patch("kiln.maker_slicer._find", return_value=None):
            # Every door asks the same question, and is answered the printer's own bed.
            assert nozzle_bed_for_slice(plate, _profile(tmp_path, _WIDE)) is None
        with pytest.raises(SlicerError, match="bed"):
            self._slice(tmp_path, plate, slicer=_orca())


class TestASlicedFileIsHeldToTheBedItWasSlicedFor:
    @pytest.fixture
    def invented_bed(self, monkeypatch):
        from kiln.printers import bed_fit

        own = bed_fit.PrinterBed((200.0, 200.0, 180.0), "invented_two", bed_fit.BED_FROM_CATALOGUE)
        monkeypatch.setattr(bed_fit, "printer_bed", lambda printer_id: own if printer_id else None)
        monkeypatch.setattr(slicer_profiles, "printer_nozzles", lambda printer_id: _WIDE)
        return bed_fit

    @pytest.mark.parametrize(
        "header, width",
        [
            ("; nozzle_diameter = 0.4,0.4\n", 260.0),
            ("; filament_map = 1,2\n", 260.0),
            ("; nozzle_diameter = 0.4\n; filament_map = 1,1\n", 200.0),
        ],
    )
    def test_by_what_the_file_states(self, tmp_path, invented_bed, header, width):
        gcode = tmp_path / "plate.gcode"
        gcode.write_text(f"{header}G28\nG1 X240 Y20 E1\n")
        assert invented_bed.sliced_file_bed(str(gcode), "invented_two").volume == (width, 200.0, 180.0)
        fit = invented_bed.validate_gcode_for_printer(str(gcode), "invented_two")
        assert fit["ok"] is (width > 240.0), fit

    def test_the_file_is_read_where_a_slicer_writes_its_figures(self, tmp_path, invented_bed, monkeypatch):
        gcode = tmp_path / "plate.gcode"
        gcode.write_text("; filament_map = 1,2\nG28\n" + "G1 X200 Y20 E0.1\n" * 3000)

        def whole(self, *a, **k):
            raise AssertionError(f"read whole: {self}")

        monkeypatch.setattr(Path, "read_text", whole)
        monkeypatch.setattr(Path, "read_bytes", whole)
        assert invented_bed.sliced_file_bed(str(gcode), "invented_two").volume == (260.0, 200.0, 180.0)


class TestAPlateIsComposedOnTheBedItPrintsOn:
    @pytest.fixture
    def invented_printer_bed(self, monkeypatch):
        """The invented printer, its maker's slicer installed here."""
        from kiln import maker_slicer
        from kiln.printers import bed_fit

        own = bed_fit.PrinterBed((200.0, 200.0, 180.0), "invented_two", bed_fit.BED_FROM_CATALOGUE)
        monkeypatch.setattr(bed_fit, "printer_bed", lambda printer_id: own if printer_id else None)
        monkeypatch.setattr(slicer_profiles, "printer_nozzles", lambda printer_id: _WIDE)
        monkeypatch.setattr(maker_slicer, "_find", lambda _slicer_id: _studio())

    def _placed_x(self, tmp_path, parts):
        from kiln.printers.bed_fit import compute_3mf_geometry_bbox

        composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / "c.3mf"), printer_id="invented_two")
        assert composed["success"], composed
        return compute_3mf_geometry_bbox(composed["output_path"])["x_max"]

    def test_two_filaments_stay_where_only_the_second_nozzle_reaches(self, tmp_path, invented_printer_bed):
        parts = [
            ColorPart(_cube(tmp_path / "a.stl", 100.0), extruder=1, name="a"),
            ColorPart(_cube(tmp_path / "b.stl", 235.0), extruder=2, name="b"),
        ]
        assert self._placed_x(tmp_path, parts) == pytest.approx(255.0)

    def test_one_filament_is_brought_back_onto_the_printers_own_bed(self, tmp_path, invented_printer_bed):
        parts = [
            ColorPart(_cube(tmp_path / "a.stl", 100.0), extruder=1, name="a"),
            ColorPart(_cube(tmp_path / "b.stl", 235.0), extruder=1, name="b"),
        ]
        assert self._placed_x(tmp_path, parts) <= 200.0

    def test_without_the_makers_slicer_two_filaments_are_laid_out_for_one_nozzle(
        self, tmp_path, invented_printer_bed, monkeypatch,
    ):
        # The plate is then sliced for one nozzle on the printer's own bed,
        # so it is laid out there too, never where only a slice for two fits.
        from kiln import maker_slicer

        monkeypatch.setattr(maker_slicer, "_find", lambda _slicer_id: None)
        parts = [
            ColorPart(_cube(tmp_path / "a.stl", 100.0), extruder=1, name="a"),
            ColorPart(_cube(tmp_path / "b.stl", 235.0), extruder=2, name="b"),
        ]
        assert self._placed_x(tmp_path, parts) <= 200.0


# ---------------------------------------------------------------------------
# The estimate before a part exists
# ---------------------------------------------------------------------------

#: An invented two-nozzle printer row with an automatic changer (invented times).
_TIMED_ROW = {
    "tool_change": {"tool_changer": "ams", "tool_change_seconds": 90},
    "nozzles": {"kind": "fixed_pair", "sliced_as": 2, "switch_seconds": 4.0, "filament_change_seconds": 40},
}


class TestTheEstimateTimesEachKindOfChange:
    @pytest.fixture
    def row(self, monkeypatch):
        from kiln import pre_estimate

        held = {"row": dict(_TIMED_ROW)}
        monkeypatch.setattr(pre_estimate, "_profile_row", lambda printer_id: held["row"] if printer_id else {})
        return held

    def _estimate(self, **kwargs):
        from kiln.pre_estimate import estimate_from_dimensions

        return estimate_from_dimensions(
            40, 40, 20, materials=["PLA", "PLA"], material_fractions=[0.5, 0.5], printer_id="invented_two",
            layer_height_mm=0.2, **kwargs,
        )

    def test_one_filament_per_nozzle_is_all_switches(self, row):
        est = self._estimate(filament_map=[1, 2])
        assert (est.nozzle_switches, est.filament_swaps) == (est.tool_changes, 0) and est.tool_changes > 0
        assert est.tool_change_time_seconds == round(est.tool_changes * 4.0)

    def test_both_on_one_nozzle_is_all_swaps(self, row):
        est = self._estimate(filament_map=[1, 1])
        assert (est.nozzle_switches, est.filament_swaps) == (0, est.tool_changes)
        assert est.tool_change_time_seconds == est.tool_changes * 40

    def test_three_filaments_on_two_nozzles_count_both(self):
        from kiln.pre_estimate import _changes_by_kind

        # Layer one: 1 -> 2 (switch), 2 -> 3 (switch and swap); back again.
        assert _changes_by_kind([1, 2, 1], 1) == (2, 1)
        assert _changes_by_kind([1, 2, 1], 2) == (4, 2)

    def test_with_no_map_every_change_keeps_the_full_time_and_says_why(self, row):
        est = self._estimate()
        assert est.tool_change_time_seconds == est.tool_changes * 90
        assert est.nozzle_switches is None and "nozzle_switches" not in est.to_dict()
        assert any("decided when the part is sliced" in note for note in est.confidence_notes)

    def test_a_one_nozzle_printer_is_estimated_exactly_as_before(self, row):
        row["row"] = {"tool_change": dict(_TIMED_ROW["tool_change"])}
        est = self._estimate()
        assert est.tool_change_time_seconds == est.tool_changes * 90
        assert not any("nozzle" in note for note in est.confidence_notes)
        assert set(est.to_dict()) == {f for f in est.__dataclass_fields__} - {"nozzle_switches", "filament_swaps"}
        with pytest.raises(ValueError, match="sliced with one nozzle"):
            self._estimate(filament_map=[1, 2])

    def test_a_map_that_does_not_fit_is_refused(self, row):
        with pytest.raises(ValueError, match="one nozzle number for each filament"):
            self._estimate(filament_map=[1, 2, 2])


# ---------------------------------------------------------------------------
# A slicer that runs past its time limit
# ---------------------------------------------------------------------------


class TestASlicerThatRunsTooLong:
    @pytest.fixture
    def slow_slicer(self, tmp_path):
        """A stand-in slicer that records its process id and then waits far
        longer than any test allows."""
        script = tmp_path / "bin" / "BambuStudio"
        script.parent.mkdir()
        pid_file = tmp_path / "slicer.pid"
        script.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 60\n')
        script.chmod(0o755)
        return SlicerInfo(path=str(script), name="bambustudio"), pid_file

    def _gone(self, pid_file: Path) -> bool:
        import os

        if not pid_file.exists():
            # Stopped before its first line ran (a busy computer is slow to
            # start it); the runner waits on what it stops, so nothing is left.
            return True
        try:
            os.kill(int(pid_file.read_text()), 0)
        except ProcessLookupError:
            return True
        return False

    def test_slice_model_stops_it_at_the_limit_and_says_so(self, tmp_path, slow_slicer):
        import time

        info, pid_file = slow_slicer
        started = time.monotonic()
        with (
            patch("kiln.server._check_auth", return_value=None),
            patch("kiln.slicer.find_slicer", return_value=info),
        ):
            reply = _slice_model_tool()(
                input_path=_plate(tmp_path, filaments=1), output_dir=str(tmp_path / "out"), printer_id=None,
                profile=_profile(tmp_path, None), time_limit_seconds=1,
            )
        assert time.monotonic() - started < 30
        assert reply["success"] is False
        assert reply["error"]["code"] == "SLICE_TOOK_TOO_LONG"
        assert reply["error"]["retryable"] is False
        assert "took too long" in reply["error"]["message"]
        assert self._gone(pid_file)
        assert list((tmp_path / "out").glob("*.gcode")) == []

    @pytest.mark.parametrize("limit", [0, -5, True, 2.5])
    def test_a_limit_that_is_not_whole_seconds_is_refused(self, tmp_path, limit):
        with patch("kiln.server._check_auth", return_value=None), patch("kiln.slicer.find_slicer", side_effect=AssertionError):
            reply = _slice_model_tool()(input_path=_plate(tmp_path, filaments=1), printer_id=None, time_limit_seconds=limit)
        assert reply["success"] is False and reply["error"]["code"] == "INVALID_INPUT"

    def test_slice_file_raises_its_own_refusal(self, tmp_path, slow_slicer):
        from kiln.slicer import SliceTimeLimitError

        info, _pid_file = slow_slicer
        with patch("kiln.slicer.find_slicer", return_value=info), pytest.raises(SliceTimeLimitError) as refused:
            slice_file(_plate(tmp_path, filaments=1), output_dir=str(tmp_path / "out"),
                       profile=_profile(tmp_path, None), timeout=1)
        assert refused.value.code == "SLICE_TOOK_TOO_LONG"


class TestTheChangeCountIsBounded:
    @staticmethod
    def _walked(filament_map, layers):
        """Every change, walked one at a time: the count to agree with."""
        loaded: dict[int, int] = {}
        for filament, nozzle in enumerate(filament_map):
            loaded.setdefault(nozzle, filament)
        current, switches, swaps = 0, 0, 0
        order = list(range(len(filament_map)))
        for layer in range(layers):
            for filament in order if layer % 2 == 0 else order[::-1]:
                if filament == current:
                    continue
                nozzle = filament_map[filament]
                switches += nozzle != filament_map[current]
                if loaded[nozzle] != filament:
                    swaps += 1
                    loaded[nozzle] = filament
                current = filament
        return switches, swaps

    @pytest.mark.parametrize("filament_map", [[1, 2], [1, 1], [1, 2, 1], [2, 1, 2, 1], [1, 2, 2, 1, 2]])
    @pytest.mark.parametrize("layers", [0, 1, 2, 3, 7, 50])
    def test_it_agrees_with_walking_every_change(self, filament_map, layers):
        from kiln.pre_estimate import _changes_by_kind

        assert _changes_by_kind(filament_map, layers) == self._walked(filament_map, layers)

    def test_an_enormous_layer_count_returns_at_once(self):
        import signal

        from kiln.pre_estimate import _changes_by_kind

        def _too_slow(*_args):
            raise TimeoutError("the count walked every layer")

        old = signal.signal(signal.SIGALRM, _too_slow)
        signal.alarm(2)
        try:
            switches, swaps = _changes_by_kind([1, 2, 1], 10**12)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        assert (switches, swaps) == (2 * 10**12, 10**12)

    def test_a_layer_height_the_nozzle_cannot_lay_is_refused(self, monkeypatch):
        from kiln import pre_estimate

        monkeypatch.setattr(pre_estimate, "_profile_row", lambda printer_id: dict(_TIMED_ROW) if printer_id else {})
        with pytest.raises(ValueError, match="cannot be estimated"):
            pre_estimate.estimate_from_dimensions(
                40, 40, 1e6, materials=["PLA", "PLA"], printer_id="invented_two", layer_height_mm=1e-9,
                filament_map=[1, 2],
            )


class TestTheTimeLimitOnlyShortens(TestTheSliceModelDoor):
    @pytest.mark.parametrize("limit", [301, 3600, 10**9])
    def test_a_limit_past_kilns_own_is_refused(self, tmp_path, limit):
        from kiln.slicer import SLICE_TIME_LIMIT_S

        reply = self._call(tmp_path, _never_runs, time_limit_seconds=limit)
        assert reply["success"] is False and reply["error"]["code"] == "INVALID_INPUT"
        assert f"from 1 to {SLICE_TIME_LIMIT_S}" in reply["error"]["message"]

    @pytest.mark.parametrize("limit", [7, 300])
    def test_a_limit_within_it_reaches_the_slicer(self, tmp_path, limit):
        seen: dict = {}
        reply = self._call(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), time_limit_seconds=limit)
        assert reply["success"], reply
        assert seen["timeout"] == limit

    def test_with_none_the_slicer_runs_under_kilns_own_limit(self, tmp_path):
        from kiln.slicer import SLICE_TIME_LIMIT_S

        seen: dict = {}
        self._call(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen))
        assert seen["timeout"] == SLICE_TIME_LIMIT_S == 300


# ---------------------------------------------------------------------------
# The nozzles the owner fitted
# ---------------------------------------------------------------------------

#: An invented machine that ships with one nozzle and takes up to three,
#: each position stating where it reaches (invented numbers), served with
#: the settings for slicing the nozzles fitted (placeholders).
_ADD_ON = {
    "kind": "rack",
    "stock": 1,
    "max": 3,
    "sliced_as": 1,
    "bed": "0x0,260x0,260x200,0x200",
    "positions": [
        {"id": 1, "reach": "0x0,230x0,230x200,0x200"},
        {"id": 2, "reach": "30x0,260x0,260x200,30x200"},
        {"id": 3, "reach": "30x0,230x0,230x200,30x200"},
    ],
    "switch_seconds": 4.0,
    "filament_change_seconds": 40,
    "maker_slicer": {"name": "bambu_studio"},
    "preset_extras": _NOZZLES["preset_extras"],
}


def _fitted(*nozzles: tuple) -> list[dict]:
    """The owner's statement: ``(id, size, material)`` per fitted nozzle."""
    return [{"id": i, "fitted": True, "diameter_mm": size, "material": material} for i, size, material in nozzles]


_TWO_HARD = _fitted((1, 0.4, "hardened_steel"), (2, 0.4, "hardened_steel"))
_HARD_AND_BRASS = _fitted((1, 0.4, "hardened_steel"), (2, 0.4, "brass"))


@pytest.fixture
def no_kiln_pro(monkeypatch):
    """Public Kiln as a customer has it: kiln-pro cannot be imported."""
    import sys

    monkeypatch.setitem(sys.modules, "kiln_pro", None)


class TestTheRecordSaysWhatIsFitted:
    """The owner's statement of each nozzle position, read from the nozzle record."""

    def test_the_served_record_brings_the_positions(self, monkeypatch, no_kiln_pro):
        from kiln import _pro_nozzle_bridge as bridge

        monkeypatch.setattr(bridge, "_record_memo", {})
        monkeypatch.setattr(bridge, "_state_memo", {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        stated = [
            {"id": 2, "fitted": True, "diameter_mm": 0.6, "material": "Brass"},
            {"id": 1, "fitted": True, "diameter_mm": 0.6, "material": "hardened_steel"},
            {"id": 3, "fitted": False},
        ]
        monkeypatch.setattr(
            "kiln.server._pro_api_call",
            lambda tool_name, _timeout=30.0, **kwargs: {
                "success": True, "found": True, "trusted_for_verdicts": True, "diameter_mm": 0.6,
                "positions": stated,
            },
        )
        assert bridge.consult_recorded_nozzle("shop_two")["positions"] == [
            {"id": 1, "fitted": True, "diameter_mm": 0.6, "material": "hardened_steel"},
            {"id": 2, "fitted": True, "diameter_mm": 0.6, "material": "brass"},
            {"id": 3, "fitted": False, "diameter_mm": None, "material": None},
        ]

    def test_a_record_nobody_stated_brings_none(self, monkeypatch, no_kiln_pro):
        from kiln import _pro_nozzle_bridge as bridge

        monkeypatch.setattr(bridge, "_record_memo", {})
        monkeypatch.setattr(bridge, "_state_memo", {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        monkeypatch.setattr(
            "kiln.server._pro_api_call",
            lambda tool_name, _timeout=30.0, **kwargs: {
                "success": True, "found": False, "trusted_for_verdicts": False, "positions": _TWO_HARD,
            },
        )
        assert bridge.consult_recorded_nozzle("shop_two")["positions"] is None

    def test_kiln_pro_beside_it_brings_the_positions(self, monkeypatch):
        import enum
        import sys
        import types

        from kiln import _pro_nozzle_bridge as bridge

        class Material(enum.Enum):
            HARDENED = "hardened_steel"

        state = types.SimpleNamespace(
            diameter_mm=0.4,
            positions=[types.SimpleNamespace(id=1, fitted=True, diameter_mm=0.4, material=Material.HARDENED)],
        )
        store = types.ModuleType("kiln_pro.nozzle_intelligence.store_resolver")
        store.recorded_nozzle = lambda printer_id, *, tool_name: state
        package = types.ModuleType("kiln_pro.nozzle_intelligence")
        package.store_resolver = store
        root = types.ModuleType("kiln_pro")
        root.nozzle_intelligence = package
        monkeypatch.setitem(sys.modules, "kiln_pro", root)
        monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence", package)
        monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence.store_resolver", store)
        assert bridge.consult_recorded_nozzle("shop_two") == {
            "diameter_mm": 0.4,
            "material": None,
            "answered": True,
            "positions": [{"id": 1, "fitted": True, "diameter_mm": 0.4, "material": "hardened_steel"}],
        }

    def test_an_entry_that_does_not_read_is_left_out(self):
        from kiln._pro_nozzle_bridge import recorded_positions_of

        read = recorded_positions_of(
            [{"id": 0, "fitted": True}, {"id": True}, {"id": "1"}, {"id": 1, "fitted": True, "diameter_mm": 9},
             {"id": 1, "fitted": False}, "nozzle"],
        )
        assert read == [{"id": 1, "fitted": True, "diameter_mm": None, "material": None}]
        assert recorded_positions_of([]) is None and recorded_positions_of(None) is None


class TestHowManyNozzlesASliceUses:
    """The one helper every door reads: the owner's fitted nozzles, bounded."""

    def test_the_fitted_nozzles_count_on_a_printer_that_ships_with_one(self):
        assert nozzle_positions(_ADD_ON) == 1
        two = slicer_profiles.fitted_record(_ADD_ON, _fitted((1, 0.4, "brass"), (2, 0.4, "brass")))
        assert nozzle_positions(two) == 2
        assert slicer_profiles.nozzle_reaches(two) == [(0.0, 0.0, 230.0, 200.0), (30.0, 0.0, 260.0, 200.0)]

    def test_one_fitted_on_a_two_nozzle_printer_slices_one(self):
        assert slicer_profiles.fitted_record(_WIDE, _fitted((1, 0.4, "brass"))) is None

    def test_no_statement_is_the_record_as_it_was(self):
        assert slicer_profiles.fitted_record(_WIDE, None) == _WIDE
        assert slicer_profiles.fitted_record(_WIDE, [{"id": 1, "fitted": False}]) == _WIDE
        assert slicer_profiles.fitted_record(_ADD_ON, None) is None

    @pytest.mark.parametrize(
        "record, fitted, used",
        [
            ({**_ADD_ON, "max": 2}, (1, 2, 3), 2),
            (_ADD_ON, (1, 3), 1),
            ({**_ADD_ON, "positions": [*_ADD_ON["positions"][:1], {"id": 2, "reach": "wide"}]}, (1, 2), 1),
            (_ADD_ON, (1, 2, 3), 3),
        ],
    )
    def test_bounded_by_the_record_and_by_the_reaches_it_states(self, record, fitted, used):
        merged = slicer_profiles._with_fitted(record, _fitted(*((i, 0.4, "brass") for i in fitted)))
        assert nozzle_positions(merged) == used

    def test_more_fitted_than_a_slice_uses_is_said_in_one_sentence(self):
        record = slicer_profiles.fitted_record(
            {**_ADD_ON, "max": 2}, _fitted(*((i, 0.4, "brass") for i in (1, 2, 3))),
        )
        said = slicer_profiles.unused_fitted_note(record)
        assert said == "This slice uses 2 of the 3 nozzles fitted (nozzles 1 and 2); the others are not used."

    def test_a_slice_for_one_of_several_names_no_nozzle(self):
        record = slicer_profiles.fitted_record(_ADD_ON, _fitted((1, 0.4, "brass"), (3, 0.4, "brass")))
        said = slicer_profiles.unused_fitted_note(record)
        assert said == "This slice is for one nozzle of the 2 fitted; the others are not used."
        assert nozzles_of(None) is None and nozzle_positions(record) == 1

    def test_nothing_is_said_when_every_fitted_nozzle_is_used(self):
        assert slicer_profiles.unused_fitted_note(slicer_profiles.fitted_record(_WIDE, _TWO_HARD)) == ""
        assert slicer_profiles.unused_fitted_note(_WIDE) == ""

    def test_more_than_the_record_is_sliced_for_needs_the_settings_for_them(self):
        # A printer whose record is sliced for one nozzle and carries no
        # settings for slicing more stays at one however many the owner
        # fitted, and the slice says so; the same record carrying them
        # slices every fitted nozzle.
        bare = {key: value for key, value in _ADD_ON.items() if key != "preset_extras"}
        both = _fitted((1, 0.4, "brass"), (2, 0.4, "brass"))
        record = slicer_profiles.fitted_record({**bare, "stock": 2, "max": 2}, both)
        assert nozzle_positions(record) == 1
        assert slicer_profiles.nozzle_bed(record) is None and slicer_profiles.nozzle_reaches(record) is None
        assert slicer_profiles.unused_fitted_note(record) == (
            "This slice is for one nozzle of the 2 fitted; the others are not used."
        )
        assert nozzle_positions(slicer_profiles.fitted_record(_ADD_ON, both)) == 2

    def test_one_size_is_the_size_and_two_sizes_are_refused(self):
        assert slicer_profiles.fitted_size_mm(slicer_profiles._with_fitted(_WIDE, _fitted((1, 0.6, "brass"), (2, 0.6, "brass")))) == 0.6
        mixed = slicer_profiles._with_fitted(_WIDE, _fitted((1, 0.4, "brass"), (2, 0.6, "brass")))
        assert slicer_profiles.fitted_size_mm(mixed) is None
        said = slicer_profiles.mixed_size_parts(mixed).message()
        assert "Nozzle 1 is 0.4 mm and nozzle 2 is 0.6 mm" in said and "Nothing was sliced" in said
        assert slicer_profiles.mixed_size_parts(slicer_profiles._with_fitted(_WIDE, _TWO_HARD)) is None


@pytest.fixture
def owner_says(monkeypatch, no_kiln_pro):
    """The invented machine's nozzle record answers with the owner's
    statement set here and no one size of its own; returns a setter."""
    from kiln import _pro_nozzle_bridge as bridge

    said: dict = {"positions": None, "material": None}

    def answer(printer_id):
        return {"diameter_mm": None, "material": said["material"], "answered": True, "positions": said["positions"]}

    monkeypatch.setattr(bridge, "consult_recorded_nozzle", answer)

    def set_positions(positions, material=None):
        said["positions"], said["material"] = positions, material

    return set_positions


class TestTheProfileIsWrittenForTheFittedNozzles:
    def test_nozzles_added_to_a_printer_that_ships_with_one_are_sliced(self, invented_printer, owner_says):
        invented_printer(_ADD_ON)
        assert nozzles_of(resolve_slicer_profile("invented_two")) is None
        owner_says(_fitted((1, 0.4, "brass"), (2, 0.4, "brass")))
        carried = nozzles_of(resolve_slicer_profile("invented_two"))
        assert carried is not None and nozzle_positions(carried) == 2
        assert nozzles_of(slicer_profiles.resolve_multiextruder_profile("invented_two", 2)) == carried

    def test_one_fitted_on_a_two_nozzle_printer_is_written_for_one(self, invented_printer, owner_says):
        invented_printer(_WIDE)
        owner_says(_fitted((1, 0.4, "brass")))
        assert nozzles_of(resolve_slicer_profile("invented_two")) is None

    def test_every_position_is_sliced_at_the_size_fitted(self, invented_printer, owner_says):
        from kiln.slicer_orca import ini_to_settings

        invented_printer(_WIDE)
        owner_says(_fitted((1, 0.6, "brass"), (2, 0.6, "brass")))
        path = resolve_slicer_profile("invented_two")
        assert ini_to_settings(path)["nozzle_diameter"] == "0.6"
        assert settings_to_orca_presets(ini_to_settings(path), nozzles=nozzles_of(path)).machine[
            "nozzle_diameter"
        ] == ["0.6", "0.6"]

    def test_the_bed_a_plate_is_laid_out_on_follows_what_is_fitted(self, invented_printer, owner_says):
        invented_printer(_WIDE)
        owner_says(_fitted((1, 0.4, "brass")))
        assert slicer_profiles.printer_nozzles("invented_two") is None
        invented_printer(_ADD_ON)
        owner_says(_fitted((1, 0.4, "brass"), (2, 0.4, "brass")))
        assert slicer_profiles.nozzle_bed(slicer_profiles.printer_nozzles("invented_two")) == (0.0, 0.0, 260.0, 200.0)

    def test_the_owners_statement_is_read_for_the_machine_a_slice_is_for(self, invented_printer, monkeypatch):
        # The statement is kept under the machine the owner named, not the
        # model: the bed a plate is laid out on reads it there, as a slice does.
        import sys

        from kiln import _pro_nozzle_bridge as bridge
        from kiln import assumed_nozzle

        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        invented_printer(_ADD_ON)
        both = _fitted((1, 0.4, "brass"), (2, 0.4, "brass"))
        monkeypatch.setattr(assumed_nozzle, "_registered_machines", lambda: ["shop_two"])
        monkeypatch.setattr(assumed_nozzle, "_model_key_of", lambda name: "invented_two" if name == "shop_two" else None)
        monkeypatch.setattr(
            bridge, "consult_recorded_nozzle",
            lambda printer_id: {
                "diameter_mm": None, "material": None, "answered": True,
                "positions": both if printer_id == "shop_two" else None,
            },
        )
        assert nozzle_positions(nozzles_of(resolve_slicer_profile("invented_two"))) == 2
        assert nozzle_positions(slicer_profiles.printer_nozzles("invented_two")) == 2

    def test_a_machine_named_among_two_of_its_model_reads_its_own_statement(self, invented_printer, monkeypatch):
        # Two machines of one model: the plate for the one named reads what
        # its owner stated on it, never neither machine's.
        import sys

        from kiln import _pro_nozzle_bridge as bridge
        from kiln import assumed_nozzle

        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        invented_printer(_ADD_ON)
        both = _fitted((1, 0.4, "brass"), (2, 0.4, "brass"))
        machines = ["invented_two_left", "invented_two_right"]
        monkeypatch.setattr(assumed_nozzle, "_registered_machines", lambda: machines)
        monkeypatch.setattr(assumed_nozzle, "_model_key_of", lambda name: "invented_two" if name in machines else None)
        monkeypatch.setattr(slicer_profiles, "_catalogue_here", lambda: False)
        monkeypatch.setattr(slicer_profiles, "_fetch_profile", lambda spelling: None)
        monkeypatch.setattr(
            bridge, "consult_recorded_nozzle",
            lambda printer_id: {
                "diameter_mm": None, "material": None, "answered": True,
                "positions": both if printer_id == "invented_two_right" else None,
            },
        )
        assert slicer_profiles.get_slicer_profile("invented_two_right").id == "invented_two"
        assert nozzle_positions(slicer_profiles.printer_nozzles("invented_two_right")) == 2
        assert slicer_profiles.printer_nozzles("invented_two_left") is None

    def test_the_one_nozzle_fitted_is_the_size_of_a_slice_for_one(self, invented_printer, owner_says):
        from kiln.slicer_orca import ini_to_settings

        invented_printer(_WIDE)
        owner_says(_fitted((2, 0.6, "brass")))
        path = resolve_slicer_profile("invented_two")
        assert nozzles_of(path) is None
        assert ini_to_settings(path)["nozzle_diameter"] == "0.6"


class TestTheSliceUsesTheFittedNozzles:
    def _slice(self, tmp_path, run, positions, *, nozzles=_WIDE, filaments=2, **kwargs):
        record = slicer_profiles.fitted_record(nozzles, positions)
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, filaments), output_dir=str(tmp_path / "out"),
                profile=_profile(tmp_path, record), **kwargs,
            )

    def test_the_slice_says_which_fitted_nozzles_it_uses(self, tmp_path):
        seen: dict = {}
        result = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen),
            _fitted(*((i, 0.4, "brass") for i in (1, 2, 3))), nozzles={**_ADD_ON, "max": 2},
        )
        assert seen["machine"]["nozzle_diameter"] == ["0.4", "0.4"]
        assert result.message.endswith(
            "This slice uses 2 of the 3 nozzles fitted (nozzles 1 and 2); the others are not used."
        )
        assert not re.search(r"\b(230|260|30)\b", result.message)

    def test_a_printer_kiln_slices_for_one_slices_one_however_many_are_fitted(self, tmp_path):
        # Its record carries no settings for slicing more: the plate is
        # sliced for one nozzle as before, never for two half-described ones.
        bare = {key: value for key, value in _ADD_ON.items() if key != "preset_extras"}
        seen: dict = {}
        result = self._slice(
            tmp_path, _slicer_that_writes("", seen), _fitted((1, 0.4, "brass"), (2, 0.4, "brass")),
            nozzles={**bare, "stock": 2, "max": 2},
        )
        assert seen["machine"]["nozzle_diameter"] == ["0.4"]
        assert "--filament-map" not in seen["cmd"] and "--export-3mf" not in seen["cmd"]
        assert result.filament_map is None and result.sliced_with is None
        assert result.message.endswith("This slice is for one nozzle of the 2 fitted; the others are not used.")

    def test_one_fitted_on_a_two_nozzle_printer_slices_for_one(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes("", seen), _fitted((1, 0.4, "brass")))
        assert seen["machine"]["nozzle_diameter"] == ["0.4"]
        assert "example_machine_key" not in seen["machine"]
        assert result.filament_map is None
        assert "nozzles fitted" not in result.message

    def test_nozzles_of_different_sizes_are_refused_before_slicing(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, _fitted((1, 0.4, "brass"), (2, 0.6, "brass")))
        assert refused.value.code == "MULTI_NOZZLE_MIXED_SIZES"
        assert "a pairing Kiln has not proven" in str(refused.value) and "Nothing was sliced" in str(refused.value)
        parts = refused.value.parts
        assert parts.headline == "Two nozzle sizes at once is an experiment"
        assert parts.reason.startswith("Nozzle 1 is 0.4 mm and nozzle 2 is 0.6 mm")
        assert parts.ask == slicer_profiles.MIXED_SIZES_CONFIRM

    def test_an_abrasive_filament_mapped_to_a_soft_nozzle_is_refused(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, _HARD_AND_BRASS, material="PLA-CF", filament_map=[1, 2])
        assert refused.value.code == "ABRASIVE_ON_SOFT_NOZZLE"
        said = str(refused.value)
        assert said.startswith("PLA-CF (filament 2) is an abrasive filament")
        assert "nozzle 2 (brass)" in said and "Put it on nozzle 1 in the map" in said and "Nothing was sliced" in said

    def test_the_slicers_own_map_is_refused_while_a_soft_nozzle_is_fitted(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, _HARD_AND_BRASS, material="PLA-CF")
        assert refused.value.code == "ABRASIVE_ON_SOFT_NOZZLE"
        said = str(refused.value)
        assert "the slicer may print them through nozzle 2 (brass)" in said
        assert "Give a filament-to-nozzle map with them on nozzle 1" in said
        parts = refused.value.parts
        assert parts.headline == "PLA-CF would wear out the nozzle"
        assert parts.fix == "Give a filament-to-nozzle map with them on nozzle 1."

    def test_a_soft_nozzle_of_unknown_material_is_not_wear_resistant(self, tmp_path):
        unknown = _fitted((1, 0.4, "hardened_steel"), (2, 0.4, None))
        with pytest.raises(NozzleMapError, match=r"nozzle 2 \(material not on record\)"):
            self._slice(tmp_path, _never_runs, unknown, material="PLA-CF")

    def test_no_wear_resistant_nozzle_says_to_fit_one(self, tmp_path):
        with pytest.raises(NozzleMapError, match="No nozzle this print uses is wear-resistant: fit a hardened nozzle for them"):
            self._slice(
                tmp_path, _never_runs, _fitted((1, 0.4, "brass"), (2, 0.4, "stainless_steel")), material="PLA-CF",
            )

    def test_an_abrasive_filament_on_wear_resistant_nozzles_slices(self, tmp_path):
        seen: dict = {}
        result = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), _TWO_HARD, material="PLA-CF",
        )
        assert result.success and seen["machine"]["nozzle_diameter"] == ["0.4", "0.4"]

    def test_a_plain_filament_with_a_soft_nozzle_slices(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes("; filament_map = 1,2\n", seen), _HARD_AND_BRASS)
        assert result.success

    def test_the_files_own_filament_names_one_abrasive_slot(self, tmp_path):
        composed = Path(_plate(tmp_path))
        plate = tmp_path / "stated.3mf"
        with zipfile.ZipFile(composed) as src, zipfile.ZipFile(plate, "w") as out:
            for name in src.namelist():
                out.writestr(name, src.read(name))
            out.writestr("Metadata/project_settings.config", json.dumps({"filament_type": ["PLA", "PETG-CF"]}))
        record = slicer_profiles.fitted_record(_WIDE, _HARD_AND_BRASS)
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _never_runs),
            pytest.raises(NozzleMapError) as refused,
        ):
            slice_file(
                str(plate), output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, record),
                filament_map=[1, 2],
            )
        said = str(refused.value)
        assert said.startswith("PETG-CF (filament 2) is an abrasive filament")
        assert "nozzle 1 (hardened steel)" not in said and "Put it on nozzle 1 in the map" in said

    def test_the_slice_model_door_answers_with_the_refusal(self, tmp_path, monkeypatch):
        from kiln import monitor_twin

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
        record = slicer_profiles.fitted_record(_WIDE, _HARD_AND_BRASS)
        with (
            patch("kiln.server._check_auth", return_value=None),
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _never_runs),
        ):
            reply = _slice_model_tool()(
                input_path=_plate(tmp_path), output_dir=str(tmp_path / "out"), printer_id=None,
                profile=_profile(tmp_path, record), material="PLA-CF",
            )
        assert reply["success"] is False
        assert reply.get("code") == "ABRASIVE_ON_SOFT_NOZZLE" or "ABRASIVE_ON_SOFT_NOZZLE" in json.dumps(reply)
        # The refusal laid out beside the whole message, for a door that shows it in parts.
        parts = reply["error"]["parts"]
        assert parts["headline"] == "PLA-CF would wear out the nozzle"
        assert parts["reason"] in reply["error"]["message"] and parts["ask"] == SOFT_NOZZLE_CONFIRM


def test_every_door_that_answers_a_refusal_lays_it_out():
    """The server's one error builder carries the parts of the refusal in
    flight, whichever door wrapped it, and nothing for any other error."""
    from kiln import server

    run = soft_nozzle_run_for_test()
    try:
        raise NozzleMapError(run.parts.message(), code="ABRASIVE_ON_SOFT_NOZZLE", parts=run.parts)
    except NozzleMapError as exc:
        reply = server._error_dict(f"Failed to slice and print: {exc}", code=exc.code)
    assert reply["error"]["parts"] == run.parts.to_dict()
    assert reply["error"]["message"].startswith("Failed to slice and print: PLA-CF is an abrasive filament")
    try:
        raise SlicerError("the slicer crashed")
    except SlicerError:
        assert "parts" not in server._error_dict("Failed to slice and print: the slicer crashed")["error"]


def soft_nozzle_run_for_test():
    from kiln.slicer import soft_nozzle_run

    return soft_nozzle_run([["PLA-CF"]], on_record="brass")


class TestTheEstimateCountsTheFittedNozzles:
    @pytest.fixture
    def row(self, monkeypatch):
        from kiln import pre_estimate

        held = {"row": {"tool_change": {"tool_changer": "ams", "tool_change_seconds": 90}, "nozzles": dict(_ADD_ON)}}
        monkeypatch.setattr(pre_estimate, "_profile_row", lambda printer_id: held["row"] if printer_id else {})
        return held

    def _estimate(self, **kwargs):
        from kiln.pre_estimate import estimate_from_dimensions

        return estimate_from_dimensions(
            40, 40, 20, materials=["PLA", "PLA"], material_fractions=[0.5, 0.5], printer_id="invented_two",
            layer_height_mm=0.2, **kwargs,
        )

    def test_a_second_nozzle_fitted_is_timed_as_switches(self, row):
        two = _fitted((1, 0.4, "brass"), (2, 0.4, "brass"))
        est = self._estimate(filament_map=[1, 2], fitted_positions=two)
        assert (est.nozzle_switches, est.filament_swaps) == (est.tool_changes, 0) and est.tool_changes > 0

    def test_the_estimate_door_reads_the_owners_statement(self, monkeypatch, owner_says, row):
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin

        tools: dict = {}

        class _FakeMcp:
            def tool(self, name=None, **_kwargs):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        _EstimateToolsPlugin().register(_FakeMcp())
        owner_says(_fitted((1, 0.4, "brass"), (2, 0.4, "brass")))
        reply = tools["estimate_before_design"](
            width_mm=40, depth_mm=40, height_mm=20, materials="PLA,PLA", material_fractions="0.5,0.5",
            printer_id="invented_two", layer_height_mm=0.2, filament_map=[1, 2],
        )
        assert reply["success"], reply
        assert reply["estimate"]["nozzle_switches"] > 0


class TestTheWearResistantNozzles:
    @pytest.mark.parametrize("material", ["hardened_steel", "Hardened Steel", "cht-hardened", "ruby_tip", "tungsten_carbide"])
    def test_these_take_an_abrasive_filament(self, material):
        from kiln.material_safety import is_wear_resistant_nozzle

        assert is_wear_resistant_nozzle(material)

    @pytest.mark.parametrize("material", ["brass", "cht_brass", "stainless_steel", "unknown", "custom", "", None])
    def test_these_do_not(self, material):
        from kiln.material_safety import is_wear_resistant_nozzle

        assert not is_wear_resistant_nozzle(material)


class TestEverySliceIsCheckedForAnAbrasiveFilament:
    """A slice for one nozzle is checked too, and fails closed when Kiln
    cannot tell which fitted nozzle will print."""

    def _slice(self, tmp_path, run, *, filaments=1, **kwargs):
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, filaments), output_dir=str(tmp_path / "out"),
                profile=resolve_slicer_profile("invented_two"), **kwargs,
            )

    def test_one_filament_on_a_printer_with_a_soft_nozzle_fitted_is_refused(
        self, tmp_path, invented_printer, owner_says,
    ):
        invented_printer(_WIDE)
        owner_says(_HARD_AND_BRASS)
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, material="PLA-CF")
        assert refused.value.code == "ABRASIVE_ON_SOFT_NOZZLE"
        said = str(refused.value)
        assert "can print through any nozzle fitted, including nozzle 2 (brass)" in said
        assert "Give a filament-to-nozzle map with it on nozzle 1" in said

    def test_with_every_fitted_nozzle_wear_resistant_it_slices(self, tmp_path, invented_printer, owner_says):
        invented_printer(_WIDE)
        owner_says(_TWO_HARD)
        assert self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA-CF").success

    def test_the_one_nozzle_fitted_on_a_two_nozzle_printer_is_judged(self, tmp_path, invented_printer, owner_says):
        invented_printer(_WIDE)
        owner_says(_fitted((1, 0.4, "brass")))
        with pytest.raises(NozzleMapError, match=r"the only nozzle fitted is nozzle 1 \(brass\), which it wears out quickly"):
            self._slice(tmp_path, _never_runs, material="PLA-CF")

    def test_a_printer_with_one_nozzle_on_record_is_judged_the_same_way(self, tmp_path, invented_printer, owner_says):
        invented_printer(None)
        owner_says(None, "brass")
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, material="PLA-CF")
        assert refused.value.code == "ABRASIVE_ON_SOFT_NOZZLE"
        assert "this printer's nozzle is recorded as brass" in str(refused.value)
        owner_says(None, "hardened_steel")
        assert self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA-CF").success

    def test_a_plain_filament_is_never_refused(self, tmp_path, invented_printer, owner_says):
        invented_printer(None)
        owner_says(None, "brass")
        assert self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA").success

    def test_a_printer_kiln_cannot_map_names_the_nozzle_that_can_take_it(self):
        from kiln.slicer import soft_nozzle_run

        said = soft_nozzle_run([["PLA-CF"]], fitted=_HARD_AND_BRASS, mappable=False).parts.fix
        assert said == "Only nozzle 1 can take it: fit wear-resistant nozzles in place of the others."
        assert soft_nozzle_run([["PLA-CF"]], fitted=_TWO_HARD) is None
        assert soft_nozzle_run([["PLA"]], on_record="brass") is None
        assert soft_nozzle_run([["PLA-CF"]]) is None


class TestAMultiMaterialPlateCarriesItsFilaments:
    """Each object's material, as multi_material_print hands it to the
    composer, reaches the same check."""

    def _plate(self, tmp_path, materials):
        from kiln.design_intelligence import get_material_profile

        parts = [
            ColorPart(
                _cube(tmp_path / f"m{i}.stl", 40.0 + 60.0 * i), extruder=i + 1, name=f"m{i}",
                material=get_material_profile(material).display_name,
            )
            for i, material in enumerate(materials)
        ]
        composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / "mm.3mf"))
        assert composed["success"], composed
        return composed["output_path"]

    def test_the_composed_plate_states_each_slots_filament(self, tmp_path):
        from kiln.file_metadata import slicer_settings

        plate = self._plate(tmp_path, ["pla", "cf_pla"])
        stated = slicer_settings(plate)["filament_type"].split(",")
        assert len(stated) == 2 and stated[0].startswith("PLA") and stated[1] == "Carbon Fiber PLA"

    def test_an_abrasive_object_on_a_soft_nozzle_is_refused(self, tmp_path):
        plate = self._plate(tmp_path, ["pla", "cf_pla"])
        record = slicer_profiles.fitted_record(_WIDE, _HARD_AND_BRASS)
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _never_runs),
            pytest.raises(NozzleMapError) as refused,
        ):
            slice_file(plate, output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, record), filament_map=[1, 2])
        assert str(refused.value).startswith("Carbon Fiber PLA (filament 2) is an abrasive filament")


class TestTheOwnerMaySayPrintAnyway:
    """An abrasive filament refused on a nozzle that is not wear-resistant
    slices once the owner says to print it anyway, and says so."""

    def _slice(self, tmp_path, run, *, filaments=1, **kwargs):
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, filaments), output_dir=str(tmp_path / "out"),
                profile=resolve_slicer_profile("invented_two"), **kwargs,
            )

    def test_the_refusal_says_why_what_fixes_it_and_how_to_print_anyway(self, tmp_path, invented_printer, owner_says):
        invented_printer(None)
        owner_says(None, "brass")
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, material="PLA-CF")
        said = str(refused.value)
        assert refused.value.code == "ABRASIVE_ON_SOFT_NOZZLE"
        assert said.endswith("; if the owner says to print it anyway, call again with soft_nozzle_confirmed=true.")
        assert "\n" not in said
        parts = refused.value.parts
        assert (parts.headline, parts.reason, parts.fix) == (
            "PLA-CF would wear out the nozzle",
            "PLA-CF is an abrasive filament, and this printer's nozzle is recorded as brass, which it wears out quickly.",
            "Fit a hardened nozzle, or tell Kiln which nozzle is fitted.",
        )
        assert parts.ask == SOFT_NOZZLE_CONFIRM and said == parts.message()

    def test_with_the_owners_word_it_slices_and_says_so(self, tmp_path, invented_printer, owner_says):
        invented_printer(None)
        owner_says(None, "brass")
        result = self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA-CF", soft_nozzle_confirmed=True)
        assert result.success
        assert result.soft_nozzle["confirmed_by"] == "owner"
        assert result.soft_nozzle["filaments"] == [{"filament": 1, "material": "PLA-CF"}]
        assert result.soft_nozzle["nozzles"] == [{"position": None, "material": "brass"}]
        said = (
            "On the owner's say-so, PLA-CF, an abrasive filament, prints through the nozzle on record "
            "(brass), which is not wear-resistant and wears quickly on it."
        )
        assert result.soft_nozzle["said"] == said and result.message.endswith(said)
        assert result.to_dict()["soft_nozzle"] == result.soft_nozzle

    @pytest.mark.parametrize("word", [False, "true", 1])
    def test_only_true_is_the_owners_word(self, tmp_path, invented_printer, owner_says, word):
        invented_printer(None)
        owner_says(None, "brass")
        with pytest.raises(NozzleMapError):
            self._slice(tmp_path, _never_runs, material="PLA-CF", soft_nozzle_confirmed=word)

    def test_a_slice_with_nothing_to_confirm_carries_nothing(self, tmp_path, invented_printer, owner_says):
        invented_printer(None)
        owner_says(None, "brass")
        result = self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA", soft_nozzle_confirmed=True)
        assert result.success and result.soft_nozzle is None and "say-so" not in result.message

    def test_the_sliced_files_map_names_the_nozzle_it_went_through(self, tmp_path, invented_printer, owner_says):
        invented_printer(_WIDE)
        owner_says(_HARD_AND_BRASS)
        both = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,2\n", {}), filaments=2,
            material="PLA-CF", soft_nozzle_confirmed=True,
        )
        assert both.soft_nozzle["nozzles"] == [{"position": 2, "material": "brass"}]
        assert "nozzle 2 (brass)" in both.message
        hard_only = self._slice(
            tmp_path, _slicer_that_writes("; filament_map = 1,1\n", {}), filaments=2,
            material="PLA-CF", soft_nozzle_confirmed=True,
        )
        assert hard_only.success and hard_only.soft_nozzle is None and "say-so" not in hard_only.message

    def test_the_slices_record_keeps_it_and_a_print_of_it_reports_it(
        self, tmp_path, invented_printer, owner_says, monkeypatch,
    ):
        from kiln import _pro_nozzle_bridge as bridge
        from kiln import print_lineage, server

        invented_printer(None)
        owner_says(None, "brass")
        result = self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA-CF", soft_nozzle_confirmed=True)
        answer = print_lineage.resolve(result.output_path, local_paths=[result.output_path])
        kept = answer["soft_nozzle"]
        assert kept["confirmed_by"] == "owner" and kept["at"] > 0
        assert kept["filaments"] == [{"filament": 1, "material": "PLA-CF"}]
        assert kept["nozzles"] == [{"position": None, "material": "brass"}]

        sent: list[dict] = []
        monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kw: sent.append({"tool": tool, **kw}) or {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        run = bridge.abrasive_run_of(answer["lineage_id"])
        bridge._served_odometer_report("invented_two", 5.0, "start:1", abrasive_run=run)
        assert sent[0]["tool"] == bridge.ODOMETER_TOOL
        assert sent[0]["abrasive_run"] == {"filaments": kept["filaments"], "nozzles": kept["nozzles"]}

        (tmp_path / "plain").mkdir()
        plain = self._slice(tmp_path / "plain", _slicer_that_writes("", {}), material="PLA")
        other = print_lineage.resolve(plain.output_path, local_paths=[plain.output_path])["lineage_id"]
        assert bridge.abrasive_run_of(other) is None
        bridge._served_odometer_report("invented_two", 5.0, "start:2", abrasive_run=bridge.abrasive_run_of(other))
        assert "abrasive_run" not in sent[1]

    def test_the_print_row_carries_the_word_to_the_account(self, tmp_path, invented_printer, owner_says):
        from kiln import outcome_courier, print_lineage

        invented_printer(None)
        owner_says(None, "brass")
        result = self._slice(tmp_path, _slicer_that_writes("", {}), material="PLA-CF", soft_nozzle_confirmed=True)
        lineage_id = print_lineage.resolve(result.output_path, local_paths=[result.output_path])["lineage_id"]
        row = {"version_id": "v1", "outcome": "failed", "determined_by": "observed", "lineage_id": lineage_id}
        assert outcome_courier.account_row(row, "a" * 32)["soft_nozzle"] == {
            "confirmed_by": "owner", "filaments": ["PLA-CF"], "nozzles": [{"position": None, "material": "brass"}],
        }
        (tmp_path / "p").mkdir()
        plain = self._slice(tmp_path / "p", _slicer_that_writes("", {}), material="PLA")
        other = print_lineage.resolve(plain.output_path, local_paths=[plain.output_path])["lineage_id"]
        assert "soft_nozzle" not in outcome_courier.account_row({**row, "lineage_id": other}, "b" * 32)

    def test_the_slice_model_door_takes_the_owners_word(self, tmp_path, invented_printer, owner_says, monkeypatch):
        from kiln import monitor_twin

        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
        invented_printer(None)
        owner_says(None, "brass")
        args = {
            "input_path": _plate(tmp_path, 1), "output_dir": str(tmp_path / "out"),
            "profile": resolve_slicer_profile("invented_two"), "material": "PLA-CF",
        }
        with (
            patch("kiln.server._check_auth", return_value=None),
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _slicer_that_writes("", {})),
        ):
            refused = _slice_model_tool()(**args)
            sliced = _slice_model_tool()(**args, soft_nozzle_confirmed=True)
        assert refused["success"] is False and "soft_nozzle_confirmed=true" in json.dumps(refused)
        assert sliced["success"] is True, sliced
        assert sliced["soft_nozzle"]["nozzles"] == [{"position": None, "material": "brass"}]


class TestEachFilamentIsLoadedAsItsOwn:
    """A filament setting stated once per slot gives each slot its own, and a
    plate's own filament types reach the slicer."""

    def test_one_value_per_slot_gives_each_slot_its_own(self):
        settings = {**_SETTINGS, "filament_type": "PLA;PLA-CF", "temperature": "220,260", "retract_length": "0.8"}
        slots = settings_to_orca_presets(settings, filament_colors=_COLORS).filaments
        assert [f["filament_type"] for f in slots] == [["PLA"], ["PLA-CF"]]
        assert [f["nozzle_temperature"] for f in slots] == [["220"], ["260"]]
        # A value stated once is the same in every slot.
        assert [f["hot_plate_temp"] for f in slots] == [["60"], ["60"]]

    def test_a_served_extra_keeps_its_key(self):
        nozzles = {**_NOZZLES, "preset_extras": {"filament": {"filament_flow_ratio": ["1"]}}}
        settings = {**_SETTINGS, "extrusion_multiplier": "0.95,0.98", "temperature": "220,260"}
        slots = settings_to_orca_presets(settings, filament_colors=_COLORS, nozzles=nozzles).filaments
        assert [f["filament_flow_ratio"] for f in slots] == [["1"], ["1"]]
        assert [f["nozzle_temperature"] for f in slots] == [["220"], ["260"]]

    def _stated_plate(self, tmp_path, types):
        composed = Path(_plate(tmp_path))
        plate = tmp_path / "stated.3mf"
        with zipfile.ZipFile(composed) as src, zipfile.ZipFile(plate, "w") as out:
            for name in src.namelist():
                out.writestr(name, src.read(name))
            out.writestr("Metadata/project_settings.config", json.dumps({"filament_type": types}))
        return str(plate)

    @pytest.mark.parametrize("nozzles", [None, _WIDE])
    def test_the_plates_own_types_load_each_filament_as_its_type(self, tmp_path, nozzles):
        record = slicer_profiles.fitted_record(nozzles, _TWO_HARD) if nozzles else None
        seen: dict = {}
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _slicer_that_writes("; filament_map = 1,2\n", seen)),
        ):
            result = slice_file(
                self._stated_plate(tmp_path, ["PLA", "PETG-CF"]), output_dir=str(tmp_path / "out"),
                profile=_profile(tmp_path, record),
            )
        assert result.success
        assert [f["filament_type"] for f in seen["filaments"]] == [["PLA"], ["PETG-CF"]]


class TestTheFileStatesShortFilamentTypes:
    """A filament's type reaches the sliced file in the short form a printer
    reads, never a long name."""

    @pytest.mark.parametrize(
        ("stated", "loaded"),
        [
            (["PLA (Polylactic Acid)", "Carbon Fiber PLA"], [["PLA"], ["PLA-CF"]]),
            (["PETG (Polyethylene Terephthalate Glycol)", "Carbon Fiber Nylon (PA-CF)"], [["PETG"], ["PA-CF"]]),
        ],
    )
    def test_a_plates_long_names_load_as_short_types(self, tmp_path, stated, loaded):
        seen: dict = {}
        plate = TestEachFilamentIsLoadedAsItsOwn()._stated_plate(tmp_path, stated)
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _slicer_that_writes("", seen)),
        ):
            assert slice_file(plate, output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, None)).success
        assert [f["filament_type"] for f in seen["filaments"]] == loaded

    def test_a_name_kiln_cannot_read_keeps_the_profiles_type(self, tmp_path):
        seen: dict = {}
        plate = TestEachFilamentIsLoadedAsItsOwn()._stated_plate(tmp_path, ["Mystery Blend", "Carbon Fiber PLA"])
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _slicer_that_writes("", seen)),
        ):
            assert slice_file(
                plate, output_dir=str(tmp_path / "out"), profile=_profile(tmp_path, None), material="PETG",
            ).success
        assert [f["filament_type"] for f in seen["filaments"]] == [["PETG"], ["PLA-CF"]]

    @pytest.mark.parametrize(
        ("declared", "written"), [("Carbon Fiber PLA", "PLA-CF"), ("cf_pla", "PLA-CF"), ("PETG-HF", "PETG-HF")],
    )
    def test_a_declared_long_name_is_written_short(self, declared, written):
        from kiln.slicer_filament import resolve_slice_filament

        assert resolve_slice_filament(declared).filament_type == written


class TestMultiMaterialPrintHeatsEachNozzleForItsOwn:
    """multi_material_print on a printer sliced with more than one nozzle:
    each filament's own temperatures, and the map the refusal asks for."""

    def _print(self, tmp_path, monkeypatch, *, several: bool, **kwargs):
        from kiln import server

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
        monkeypatch.setattr(server, "_refuse_undriven_multi_material", lambda *a, **k: None)
        monkeypatch.setattr(server, "_slices_for_several_nozzles", lambda *a, **k: several)
        handed: dict = {}
        monkeypatch.setattr(server, "run_reslice_and_print", lambda **kw: handed.update(kw) or {"success": True})
        reply = server.multi_material_print(
            objects_json=[
                {"file_path": _cube(tmp_path / "a.stl", 40.0), "material_id": "pla"},
                {"file_path": _cube(tmp_path / "b.stl", 100.0), "material_id": "petg"},
            ],
            auto_ams=False, **kwargs,
        )
        assert reply.get("success") is True, reply
        return handed

    def _own(self, material: str, key: str) -> str:
        from kiln.slicer_material import material_needs

        return str(material_needs(material, printer_id=None).values()[key])

    def test_a_plate_sliced_for_one_nozzle_here_is_heated_as_one(self, invented_printer, monkeypatch):
        # Without the maker's slicer the plate is sliced for one nozzle, so
        # it is heated the way a plate for one nozzle is.
        from kiln import maker_slicer, server

        invented_printer(_WIDE)
        monkeypatch.setattr(maker_slicer, "_find", lambda _slicer_id: None)
        assert server._slices_for_several_nozzles("invented_two", None) is False
        monkeypatch.setattr(maker_slicer, "_find", lambda _slicer_id: _studio())
        assert server._slices_for_several_nozzles("invented_two", None) is True

    def test_each_filament_keeps_its_own_temperatures(self, tmp_path, monkeypatch):
        handed = self._print(tmp_path, monkeypatch, several=True, filament_map=[2, 1], soft_nozzle_confirmed=True)
        overrides = json.loads(handed["overrides"])
        for key in ("temperature", "first_layer_temperature"):
            assert overrides[key] == f"{self._own('pla', key)},{self._own('petg', key)}"
        assert handed["filament_map"] == [2, 1] and handed["soft_nozzle_confirmed"] is True

    def test_the_yes_to_two_nozzle_sizes_is_handed_on_only_when_given(self, tmp_path, monkeypatch):
        handed = self._print(tmp_path, monkeypatch, several=True, mixed_nozzle_experiment=True)
        assert handed["mixed_nozzle_experiment"] is True
        handed = self._print(tmp_path, monkeypatch, several=True)
        assert handed["mixed_nozzle_experiment"] is False

    def test_one_nozzle_prints_every_filament_at_the_hottest(self, tmp_path, monkeypatch):
        handed = self._print(tmp_path, monkeypatch, several=False)
        overrides = json.loads(handed["overrides"])
        hottest = max(("pla", "petg"), key=lambda m: float(self._own(m, "temperature")))
        assert overrides["temperature"] == self._own(hottest, "temperature")
        assert handed["filament_map"] is None and handed["soft_nozzle_confirmed"] is False


class TestCopiesInSeveralColoursKeepTheOwnersWord:
    """Copies sliced one filament each and merged into one file: the merged
    file is the one that prints, so its record keeps the owner's word."""

    _SAID = "On the owner's say-so, PLA-CF, an abrasive filament, prints through the nozzle on record."

    def _merged(self, tmp_path, monkeypatch, **kwargs):
        from kiln import print_lineage
        from kiln import slicer as slicer_mod

        def one_copy(path, *, output_dir, soft_nozzle_confirmed=False, **_kw):
            out = Path(output_dir) / f"{Path(path).stem}.gcode"
            out.write_text("; header\n;LAYER_CHANGE\nG1 X1 Y1 E1\n")
            print_lineage.record_slice(path, str(out))
            result = slicer_mod.SliceResult(success=True, output_path=str(out), slicer="x", message="Sliced")
            if soft_nozzle_confirmed:
                result.soft_nozzle = {
                    "confirmed_by": "owner", "filaments": [{"filament": 1, "material": "PLA-CF"}],
                    "nozzles": [{"position": None, "material": "brass"}], "said": self._SAID,
                }
            return result

        monkeypatch.setattr(slicer_mod, "slice_file", one_copy)
        return slicer_mod.slice_multicolor_copies(
            _cube(tmp_path / "part.stl", 0.0), 2, output_dir=str(tmp_path / "out"), **kwargs,
        )

    def test_the_merged_files_record_keeps_it_and_the_print_row_carries_it(self, tmp_path, monkeypatch):
        from kiln import outcome_courier, print_lineage

        merged = self._merged(tmp_path, monkeypatch, soft_nozzle_confirmed=True)
        assert merged.soft_nozzle["filaments"] == [
            {"filament": 1, "material": "PLA-CF"}, {"filament": 2, "material": "PLA-CF"},
        ]
        assert merged.soft_nozzle["nozzles"] == [{"position": None, "material": "brass"}]
        assert merged.message.endswith(self._SAID)
        answer = print_lineage.resolve(merged.output_path, local_paths=[merged.output_path])
        assert answer["part"] == print_lineage.PART_SLICED_HERE
        assert answer["soft_nozzle"]["confirmed_by"] == "owner"
        assert answer["soft_nozzle"]["filaments"] == merged.soft_nozzle["filaments"]
        row = {"version_id": "v1", "outcome": "failed", "determined_by": "observed", "lineage_id": answer["lineage_id"]}
        assert outcome_courier.account_row(row, "a" * 32)["soft_nozzle"] == {
            "confirmed_by": "owner", "filaments": ["PLA-CF"], "nozzles": [{"position": None, "material": "brass"}],
        }

    def test_copies_nobody_said_to_print_anyway_carry_nothing(self, tmp_path, monkeypatch):
        from kiln import print_lineage

        merged = self._merged(tmp_path, monkeypatch)
        assert merged.soft_nozzle is None and "say-so" not in merged.message
        answer = print_lineage.resolve(merged.output_path, local_paths=[merged.output_path])
        assert answer["part"] == print_lineage.PART_SLICED_HERE and answer["soft_nozzle"] is None


class TestTheTerminalTakesTheOwnersWord:
    """``kiln slice`` says how to print anyway in its own spelling, and its
    flag reaches the slice."""

    def _run(self, tmp_path, monkeypatch, *args):
        from click.testing import CliRunner

        import kiln.cli.main as cli_main

        seen: dict = {}

        def placed(ctx, input_file, **kwargs):
            seen.update(kwargs)
            if not kwargs.get("soft_nozzle_confirmed"):
                raise NozzleMapError(
                    "Filament 1 is PLA-CF, an abrasive filament, and the nozzle on record for this printer is "
                    "brass, which is not wear-resistant, so nothing was sliced; fit a hardened one for it, or "
                    f"record the nozzle that is fitted; {slicer_mod.SOFT_NOZZLE_CONFIRM}.",
                    code="ABRASIVE_ON_SOFT_NOZZLE",
                )
            gcode = tmp_path / "out.gcode"
            gcode.write_text("G1 X1\n")
            return slicer_mod.SliceResult(success=True, output_path=str(gcode), slicer="x", message="Sliced"), {}

        from kiln import slicer as slicer_mod

        monkeypatch.setenv("KILN_ACCEPT_TERMS", "1")
        monkeypatch.setattr(cli_main, "_cli_placed_slice", placed)
        monkeypatch.setattr(cli_main, "_resolve_slice_plan", lambda *a, **k: {
            "extra_args": [], "printer_id": None, "profile_path": None, "material": "PLA-CF",
            "declared_material": "PLA-CF", "loaded_material": None, "loaded_determined_by": None,
        })
        out = CliRunner().invoke(cli_main.cli, ["slice", _cube(tmp_path / "part.stl", 40.0), *args])
        return out, seen

    def test_the_refusal_names_the_flag(self, tmp_path, monkeypatch):
        out, _seen = self._run(tmp_path, monkeypatch, "--json")
        said = json.loads(out.output)["error"]["message"]
        assert out.exit_code == 1
        assert said.endswith("; if the owner says to print it anyway, run it again with --soft-nozzle-confirmed.")
        assert "soft_nozzle_confirmed=true" not in said

    def test_the_flag_reaches_the_slice(self, tmp_path, monkeypatch):
        out, seen = self._run(tmp_path, monkeypatch, "--soft-nozzle-confirmed")
        assert out.exit_code == 0, out.output
        assert seen["soft_nozzle_confirmed"] is True

    def test_the_flag_for_two_nozzle_sizes_reaches_the_slice(self, tmp_path, monkeypatch):
        out, seen = self._run(tmp_path, monkeypatch, "--soft-nozzle-confirmed", "--mixed-nozzle-experiment")
        assert out.exit_code == 0, out.output
        assert seen["mixed_nozzle_experiment"] is True
        out, seen = self._run(tmp_path, monkeypatch, "--soft-nozzle-confirmed")
        assert out.exit_code == 0, out.output
        assert "mixed_nozzle_experiment" not in seen
