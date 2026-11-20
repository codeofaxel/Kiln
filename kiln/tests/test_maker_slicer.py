"""The slicer a slice for more than one nozzle is made with.

Covered here, with an invented machine, an invented settings bundle and
placeholder extras: a nozzle record names its maker's slicer and the lowest
version accepted; that slicer is used when installed and never swapped for
one the caller named; with that slicer missing or too old the print is
sliced for one nozzle instead, and the result says how many filament swaps
the file makes and, the first time on a printer or for many swaps, what to
install; a map, a slicer named while the maker's one is installed, or a
slicer without the printer's routines is refused in one sentence that names
the slicer; a slicer of PrusaSlicer's kind is handed the settings file and
plate this module writes for it; and the result and the sliced file's record
say which slicer and version made the slice.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln import maker_slicer, maker_slicer_notes, print_lineage, slicer_profiles
from kiln.maker_slicer import (
    NEEDS_MAKER_SLICER,
    named_slicer,
    ready_for,
    several_nozzle_profile,
    slicer_for_several_nozzles,
    with_tower_position,
)
from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
from kiln.slicer import NozzleMapError, OneNozzleInstead, SlicerInfo, ini_to_settings, slice_file
from kiln.slicer_orca import PresetExtrasError

from .test_slicer_multi_nozzle import _NOZZLES, _cube
from .test_slicer_multi_nozzle import _SETTINGS as _PAIR_SETTINGS
from .test_slicer_multi_nozzle import _profile as _pair_profile
from .test_slicer_multi_nozzle import _slicer_that_writes as _studio_that_writes

_SETTINGS = {
    "nozzle_diameter": "0.4",
    "retract_length": "0.8",
    "layer_height": "0.2",
    "bed_shape": "0x0,300x0,300x300,0x300",
    "temperature": "220",
    "bed_temperature": "60",
    "gcode_flavor": "marlin",
}

#: An invented settings bundle: one printer preset whose routines come from
#: two parents, the nearer parent winning.
_BUNDLE = """\
[vendor]
name = Invented
config_version = {version}

[printer:*base*]
start_gcode = BASE START
end_gcode = BASE END

[printer:*several*]
inherits = *base*
start_gcode = SEVERAL START\\nLINE TWO
toolchange_gcode = CHANGE {{next_extruder}}

[printer:Invented Three 0.4 nozzle]
inherits = *several*
nozzle_diameter = 0.4,0.4,0.4
"""

_ROUTINES = {
    "vendor": "Invented",
    "min_profile_version": "1.2.0",
    "preset": "Invented Three {nozzle} nozzle",
    "keys": ["start_gcode", "toolchange_gcode", "end_gcode"],
}

#: An invented three-nozzle machine that ships with one.
_RECORD = {
    "kind": "toolchanger",
    "stock": 1,
    "max": 3,
    "sliced_as": 1,
    "bed": "0x0,300x0,300x300,0x300",
    "positions": [{"id": i, "reach": "0x0,300x0,300x300,0x300", "max_height": 300} for i in (1, 2, 3)],
    "maker_slicer": {"name": "prusaslicer", "min_version": "2.5.0", "routines": _ROUTINES},
    "preset_extras": {
        "machine": {"example_machine_key": "m", "example_list_key": ["a", "b", "c"]},
        "process": {"wipe_tower": "1"},
        "filament": {},
    },
}
_TWO_FITTED = [
    {"id": 1, "fitted": True, "diameter_mm": 0.4, "material": "brass"},
    {"id": 2, "fitted": True, "diameter_mm": 0.4, "material": "brass"},
]


def _prusa(version: str = "2.9.4") -> SlicerInfo:
    return SlicerInfo(path="/fake/PrusaSlicer", name="prusa-slicer", version=f"PrusaSlicer-{version} based on Slic3r")


def _orca() -> SlicerInfo:
    return SlicerInfo(path="/fake/OrcaSlicer", name="orcaslicer", version="OrcaSlicer-2.3.2:")


def _studio() -> SlicerInfo:
    return SlicerInfo(path="/fake/BambuStudio", name="bambustudio")


def _bundle(folder: Path, version: str = "1.2.0") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Invented.ini"
    path.write_text(_BUNDLE.format(version=version), encoding="utf-8")
    return path


#: An invented settings bundle of the other kind: an index naming each printer
#: preset's file, and a printer preset that takes its routines from a parent
#: and from the presets it includes, an included one winning over the parent.
_PRESETS = {
    "invented common": {
        "machine_start_gcode": "COMMON START",
        "change_filament_gcode": "COMMON CHANGE",
        "machine_end_gcode": "COMMON END",
    },
    "Invented Two 0.4 nozzle start": {"machine_start_gcode": "TWO START\nLINE TWO"},
    "Invented Two 0.4 nozzle change": {
        "change_filament_gcode": "M620 S[next_filament_id]A\nT[next_filament_id]\nM621 S[next_filament_id]A",
    },
    "Invented Two 0.4 nozzle layer": {"layer_change_gcode": "LAYER NOTE {layer_num}\nLAYER NOTE TWO"},
    "Invented Two 0.4 nozzle frame": {"invented_frame_gcode": "FRAME {layer_num}"},
    "Invented Two 0.4 nozzle": {
        "inherits": "invented common",
        "include": [
            "Invented Two 0.4 nozzle start", "Invented Two 0.4 nozzle change", "Invented Two 0.4 nozzle layer",
            "Invented Two 0.4 nozzle frame",
        ],
        "nozzle_diameter": ["0.4", "0.4"],
    },
}

_PRESET_ROUTINES = {
    "vendor": "Invented",
    "min_profile_version": "01.02.00.00",
    "preset": "Invented Two {nozzle} nozzle",
    "keys": ["machine_start_gcode", "change_filament_gcode", "machine_end_gcode"],
}

#: An invented two-nozzle machine whose maker's slicer keeps its settings that way.
_PAIR = {**_NOZZLES, "maker_slicer": {"name": "bambu_studio", "routines": _PRESET_ROUTINES}}

#: The same machine, its record also naming the routine its maker runs at every layer.
_PAIR_WITH_LAYER = {
    **_NOZZLES,
    "maker_slicer": {
        "name": "bambu_studio",
        "routines": {**_PRESET_ROUTINES, "keys": [*_PRESET_ROUTINES["keys"], "layer_change_gcode"]},
    },
}


def _preset_bundle(folder: Path, version: str = "01.02.00.00", *, where: dict[str, str] | None = None) -> Path:
    """:data:`_PRESETS` written as an index and a folder of preset files;
    *where* gives a preset's file a path of its own."""
    entries = []
    for i, (name, body) in enumerate(_PRESETS.items()):
        sub_path = (where or {}).get(name, f"machine/preset_{i}.json")
        file = folder / "Invented" / sub_path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"name": name, **body}), encoding="utf-8")
        entries.append({"name": name, "sub_path": sub_path})
    index = folder / "Invented.json"
    index.write_text(json.dumps({"name": "Invented", "version": version, "machine_list": entries}), encoding="utf-8")
    return index


def _profile(tmp_path: Path, record: dict | None = None) -> str:
    """The invented machine's profile, as Kiln writes one for a printer, carrying
    its record with two nozzles fitted."""
    nozzles = slicer_profiles.fitted_record(record or _RECORD, _TWO_FITTED)
    ini = tmp_path / "invented.ini"
    body = slicer_profiles._settings_to_ini(dict(_SETTINGS), "Invented Three", printer_id="invented_three")
    ini.write_text(slicer_profiles._with_nozzles(body, nozzles))
    return str(ini)


def _plate(tmp_path: Path, filaments: int = 2) -> str:
    parts = [
        ColorPart(_cube(tmp_path / f"part{i}.stl", 40.0 + 60.0 * i), extruder=i + 1, name=f"part{i}")
        for i in range(filaments)
    ]
    composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / "plate.3mf"))
    assert composed["success"], composed
    return composed["output_path"]


# ---------------------------------------------------------------------------
# Which slicer
# ---------------------------------------------------------------------------


class TestTheNamedSlicer:
    def test_a_record_says_its_makers_slicer_and_floor(self):
        named = named_slicer(_RECORD)
        assert (named["name"], named["min_version"]) == ("prusaslicer", "2.5.0")
        assert named["routines"]["vendor"] == "Invented"

    @pytest.mark.parametrize("record", [{}, None, {"maker_slicer": {"name": "nobody's"}}, {"maker_slicer": "x"}])
    def test_a_record_that_names_no_slicer_kiln_knows_is_refused_never_guessed(self, record):
        with pytest.raises(NozzleMapError) as refused:
            named_slicer(record)
        assert not isinstance(refused.value, OneNozzleInstead)
        assert refused.value.code == NEEDS_MAKER_SLICER
        said = str(refused.value)
        # One plain sentence, naming no slicer.
        assert said.endswith(".") and ". " not in said and "nothing was sliced" in said
        assert not any(known.said in said for known in maker_slicer._KNOWN.values())

    def test_whether_a_slicer_is_ready_is_never_guessed_either(self):
        with pytest.raises(NozzleMapError) as refused:
            ready_for({"kind": "fixed_pair", "sliced_as": 2})
        assert refused.value.code == NEEDS_MAKER_SLICER


class TestWhichSlicerMakesTheSlice:
    def _choose(self, tmp_path, current, *, record=None, chosen=False, found=None, bundles=None):
        places = [_bundle(tmp_path / "app")] if bundles is None else bundles
        with (
            patch("kiln.maker_slicer._find", return_value=found),
            patch("kiln.maker_slicer._vendor_files", return_value=places),
        ):
            return slicer_for_several_nozzles(record or _RECORD, current, chosen=chosen, profile=_profile(tmp_path))

    def test_the_makers_slicer_in_hand_is_used_and_named(self, tmp_path):
        several = self._choose(tmp_path, _prusa())
        assert several.info.path == "/fake/PrusaSlicer"
        assert several.sliced_with == {
            "slicer": "prusaslicer", "name": "PrusaSlicer", "version": "2.9.4",
            "profile": "Invented", "profile_version": "1.2.0",
        }

    def test_another_slicer_in_hand_is_swapped_for_the_installed_makers_one(self, tmp_path):
        several = self._choose(tmp_path, _orca(), found=_prusa())
        assert several.info.path == "/fake/PrusaSlicer"

    def test_a_slicer_the_caller_named_is_never_swapped(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _orca(), chosen=True, found=_prusa())
        assert refused.value.code == NEEDS_MAKER_SLICER

    def test_with_the_makers_slicer_missing_the_refusal_names_it(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _orca())
        said = str(refused.value)
        assert refused.value.code == NEEDS_MAKER_SLICER
        assert "PrusaSlicer" in said and "install" in said and "nothing was sliced" in said
        assert "Bambu Studio" not in said

    @pytest.mark.parametrize("current", [_prusa(), _studio()])
    def test_a_record_that_names_none_is_refused_whatever_slicer_is_in_hand(self, tmp_path, current):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, current, record={"kind": "fixed_pair", "sliced_as": 2}, found=_studio())
        assert not isinstance(refused.value, OneNozzleInstead)
        assert refused.value.code == NEEDS_MAKER_SLICER and "Bambu Studio" not in str(refused.value)

    def test_a_slicer_older_than_the_record_accepts_is_refused_with_both_versions(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _prusa("2.4.9"))
        said = str(refused.value)
        assert "2.4.9" in said and "2.5.0" in said and "nothing was sliced" in said

    def test_a_missing_or_old_makers_slicer_is_a_miss_a_slice_can_make_for_one_nozzle(self, tmp_path):
        with pytest.raises(OneNozzleInstead) as old:
            self._choose(tmp_path, _prusa("2.4.9"))
        assert (old.value.miss.need, old.value.miss.have, old.value.miss.floor) == ("update", "2.4.9", "2.5.0")
        with pytest.raises(OneNozzleInstead) as missing:
            self._choose(tmp_path, _orca())
        assert (missing.value.miss.slicer, missing.value.miss.need) == ("prusaslicer", "install")
        with pytest.raises(OneNozzleInstead) as old_bundle:
            self._choose(tmp_path, _prusa(), bundles=[_bundle(tmp_path / "app", "1.1.9")])
        assert old_bundle.value.miss.need == "update" and old_bundle.value.miss.floor is None

    def test_a_named_slicer_while_the_makers_is_installed_is_no_miss_but_a_refusal(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _orca(), chosen=True, found=_prusa())
        assert not isinstance(refused.value, OneNozzleInstead)
        assert "PrusaSlicer is installed" in str(refused.value)

    @pytest.mark.parametrize("version", ["2.5.0", "2.5", "2.10.0", "3.0"])
    def test_a_slicer_at_or_above_the_floor_is_accepted(self, tmp_path, version):
        assert self._choose(tmp_path, _prusa(version)).info is not None


# ---------------------------------------------------------------------------
# The routines
# ---------------------------------------------------------------------------


class TestTheRoutines:
    _choose = TestWhichSlicerMakesTheSlice._choose

    def test_they_are_read_through_the_presets_parents(self, tmp_path):
        routines = self._choose(tmp_path, _prusa()).routines
        assert routines == {
            "start_gcode": "SEVERAL START\\nLINE TWO",
            "toolchange_gcode": "CHANGE {next_extruder}",
            "end_gcode": "BASE END",
        }

    def test_the_newest_copy_of_the_bundle_is_the_one_read(self, tmp_path):
        older, newer = _bundle(tmp_path / "app", "1.2.0"), _bundle(tmp_path / "mine", "1.10.0")
        several = self._choose(tmp_path, _prusa(), bundles=[older, newer])
        assert several.sliced_with["profile_version"] == "1.10.0"

    def test_a_bundle_older_than_the_record_accepts_is_refused(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _prusa(), bundles=[_bundle(tmp_path / "app", "1.1.9")])
        assert "1.1.9" in str(refused.value) and "1.2.0" in str(refused.value)

    def test_no_bundle_is_refused(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _prusa(), bundles=[])
        assert refused.value.code == NEEDS_MAKER_SLICER

    def test_a_nozzle_size_the_bundle_has_no_preset_for_is_refused(self, tmp_path):
        record = {**_RECORD, "maker_slicer": {**_RECORD["maker_slicer"], "routines": {**_ROUTINES, "preset": "Other {nozzle}"}}}
        with pytest.raises(NozzleMapError) as refused:
            self._choose(tmp_path, _prusa(), record=record)
        assert "no settings for this printer" in str(refused.value)

    @pytest.mark.parametrize("keys", [["post_process"], ["start_gcode", "notes"], [], "start_gcode"])
    def test_only_routines_are_read(self, tmp_path, keys):
        record = {**_RECORD, "maker_slicer": {**_RECORD["maker_slicer"], "routines": {**_ROUTINES, "keys": keys}}}
        with pytest.raises(NozzleMapError):
            self._choose(tmp_path, _prusa(), record=record)

    def test_the_bundle_is_looked_for_beside_the_program(self, tmp_path):
        app = tmp_path / "Invented.app" / "Contents"
        (app / "MacOS").mkdir(parents=True)
        binary = app / "MacOS" / "PrusaSlicer"
        binary.write_text("")
        bundle = _bundle(app / "Resources" / "profiles")
        found = maker_slicer._vendor_files(str(binary), "Invented", "prusaslicer")
        assert bundle.resolve() in [p.resolve() for p in found]

    # --- a maker's slicer that keeps each printer preset in a file of its own ---

    def _choose_pair(self, tmp_path, bundles):
        with (
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.maker_slicer._vendor_files", return_value=bundles),
        ):
            return slicer_for_several_nozzles(_PAIR, _studio(), chosen=False, profile=_pair_profile(tmp_path, _PAIR))

    def test_preset_files_are_read_through_parents_and_included_presets(self, tmp_path):
        several = self._choose_pair(tmp_path, [_preset_bundle(tmp_path / "app")])
        assert several.routines == {
            "machine_start_gcode": "TWO START\nLINE TWO",
            "change_filament_gcode": "M620 S[next_filament_id]A\nT[next_filament_id]\nM621 S[next_filament_id]A",
            "machine_end_gcode": "COMMON END",
        }
        assert (several.sliced_with["profile"], several.sliced_with["profile_version"]) == ("Invented", "1.2.0.0")

    def test_the_newest_copy_of_the_preset_files_is_the_one_read(self, tmp_path):
        older = _preset_bundle(tmp_path / "app", "01.02.00.00")
        newer = _preset_bundle(tmp_path / "mine", "01.02.00.09")
        assert self._choose_pair(tmp_path, [older, newer]).sliced_with["profile_version"] == "1.2.0.9"

    def test_preset_files_older_than_the_record_accepts_are_a_miss_to_update(self, tmp_path):
        with pytest.raises(OneNozzleInstead) as old:
            self._choose_pair(tmp_path, [_preset_bundle(tmp_path / "app", "01.01.09.00")])
        assert old.value.miss.need == "update"

    def test_a_preset_file_outside_its_folder_is_never_read(self, tmp_path):
        outside = tmp_path / "app" / "elsewhere.json"
        outside.parent.mkdir(parents=True)
        outside.write_text(json.dumps({"machine_start_gcode": "NOT THE MAKER'S"}), encoding="utf-8")
        index = _preset_bundle(tmp_path / "app", where={"Invented Two 0.4 nozzle start": "../elsewhere.json"})
        with pytest.raises(NozzleMapError) as refused:
            self._choose_pair(tmp_path, [index])
        assert "no settings for this printer" in str(refused.value)

    def test_the_preset_files_are_looked_for_beside_the_program_and_in_the_persons_own_settings(
        self, tmp_path, monkeypatch,
    ):
        contents = tmp_path / "Invented.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        binary = contents / "MacOS" / "BambuStudio"
        binary.write_text("")
        beside = _preset_bundle(contents / "Resources" / "profiles")
        monkeypatch.setattr(maker_slicer, "sys", SimpleNamespace(platform="darwin"))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        own = _preset_bundle(tmp_path / "home" / "Library" / "Application Support" / "BambuStudio" / "system")
        found = maker_slicer._vendor_files(str(binary), "Invented", "bambu_studio")
        # A file system that ignores case finds the copy beside the program twice.
        assert {os.stat(p).st_ino for p in found} == {os.stat(beside).st_ino, os.stat(own).st_ino}


# ---------------------------------------------------------------------------
# The settings file and the plate
# ---------------------------------------------------------------------------


class TestTheSettingsFile:
    def test_extras_sizes_and_routines_are_laid_over_the_profile(self, tmp_path):
        made = several_nozzle_profile(_profile(tmp_path), _RECORD, 2, {"start_gcode": "S", "toolchange_gcode": "C"})
        settings = ini_to_settings(made)
        assert settings["example_machine_key"] == "m"
        assert settings["example_list_key"] == "a,b,c"
        assert settings["wipe_tower"] == "1"
        assert settings["nozzle_diameter"] == "0.4,0.4"
        assert (settings["start_gcode"], settings["toolchange_gcode"]) == ("S", "C")
        assert settings["retract_length"] == "0.8"
        assert slicer_profiles.nozzles_of(made) is not None

    def test_an_extra_that_commands_a_heater_is_refused(self, tmp_path):
        record = {**_RECORD, "preset_extras": {"machine": {"temperature": "300"}}}
        with pytest.raises(PresetExtrasError):
            several_nozzle_profile(_profile(tmp_path), record, 2, {})


class TestTheTowersPlace:
    def test_the_copy_says_where_the_tower_stands_and_keeps_the_rest(self, tmp_path):
        plate = _plate(tmp_path)
        placed = str(tmp_path / "placed.3mf")
        assert with_tower_position(plate, placed, (210.0, 40.5)) is True
        with zipfile.ZipFile(plate) as before, zipfile.ZipFile(placed) as after:
            assert set(after.namelist()) == set(before.namelist()) | {maker_slicer._TOWER_MEMBER}
            said = after.read(maker_slicer._TOWER_MEMBER).decode()
            assert 'position_x="210.000"' in said and 'position_y="40.500"' in said
            for name in before.namelist():
                assert after.read(name) == before.read(name)

    def test_a_file_that_is_no_3mf_is_left_alone(self, tmp_path):
        bad = tmp_path / "bad.3mf"
        bad.write_text("not a zip")
        assert with_tower_position(str(bad), str(tmp_path / "out.3mf"), (1.0, 2.0)) is False
        assert not (tmp_path / "out.3mf").exists()


# ---------------------------------------------------------------------------
# Through the slice
# ---------------------------------------------------------------------------


def _slicer_that_writes(seen: dict):
    def run(cmd, *, timeout, retryable_returncodes, remove_partial_output, output_is_complete, cwd=None):
        seen["cmd"] = list(cmd)
        seen["settings"] = ini_to_settings(cmd[cmd.index("--load") + 1])
        seen["input"] = cmd[cmd.index("--export-gcode") + 1]
        with zipfile.ZipFile(seen["input"]) as plate:
            seen["members"] = plate.namelist()
            seen["tower"] = (
                plate.read(maker_slicer._TOWER_MEMBER).decode() if maker_slicer._TOWER_MEMBER in seen["members"] else None
            )
        Path(cmd[cmd.index("--output") + 1]).write_text("G28\nT0\nG1 X1 Y1 E1\nT1\nG1 X2 Y2 E1\n; done\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def _slicer_swapping(seen: dict, swaps: int, *, tools: int = 2):
    """A PrusaSlicer stand-in whose sliced file changes filament *swaps* times,
    or with *tools* 1 selects one tool only (a plate folded into one colour)."""
    body = "".join(f"T{i % tools}\nG1 X{i} Y1 E1\n" for i in range(swaps + 1))

    def run(cmd, *, timeout, retryable_returncodes, remove_partial_output, output_is_complete, cwd=None):
        seen["settings"] = ini_to_settings(cmd[cmd.index("--load") + 1])
        seen["input"] = cmd[cmd.index("--export-gcode") + 1]
        with zipfile.ZipFile(seen["input"]) as plate:
            seen["tower"] = maker_slicer._TOWER_MEMBER in plate.namelist()
        Path(cmd[cmd.index("--output") + 1]).write_text(f"G28\n{body}; done\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def _never_runs(*_args, **_kwargs):
    raise AssertionError("the slicer was launched")


@pytest.fixture
def notes(tmp_path, monkeypatch):
    """What Kiln said kept in a file of the test's own, on a computer with no Homebrew."""
    monkeypatch.setattr(maker_slicer_notes, "store_path", lambda: tmp_path / "notes.json")
    monkeypatch.setattr(maker_slicer, "_brew_command", lambda *_a, **_k: None)
    return tmp_path / "notes.json"


class TestTheSlice:
    def _slice(self, tmp_path, run, *, slicer=None, found=None, filaments=2, record=None, **kwargs):
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer or _prusa()),
            patch("kiln.maker_slicer._find", return_value=found),
            patch("kiln.maker_slicer._vendor_files", return_value=[_bundle(tmp_path / "app")]),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._gcode_output_is_complete", return_value=True),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, filaments), output_dir=str(tmp_path / "out"),
                profile=_profile(tmp_path, record), **kwargs,
            )

    def test_the_makers_slicer_is_handed_extras_sizes_and_its_own_routines(self, tmp_path):
        seen: dict = {}
        self._slice(tmp_path, _slicer_that_writes(seen))
        settings = seen["settings"]
        assert settings["example_machine_key"] == "m"
        assert settings["nozzle_diameter"] == "0.4,0.4"
        assert settings["start_gcode"] == "SEVERAL START\\nLINE TWO"
        assert settings["toolchange_gcode"] == "CHANGE {next_extruder}"
        assert settings["end_gcode"] == "BASE END"

    def test_the_tower_is_stood_clear_of_the_parts(self, tmp_path):
        seen: dict = {}
        self._slice(tmp_path, _slicer_that_writes(seen))
        assert seen["tower"] is not None
        import re

        x, y = (float(re.search(rf'position_{axis}="([\d.]+)"', seen["tower"]).group(1)) for axis in "xy")
        # The two cubes span X 40-120 and Y 90-110; the tower's square is past them.
        assert x >= 120.0 or y >= 110.0 or x + 60.0 <= 40.0 or y + 60.0 <= 90.0
        assert 0 <= x <= 240.0 and 0 <= y <= 240.0

    def test_the_result_and_the_files_record_say_which_slicer_made_it(self, tmp_path):
        noted: dict = {}
        with patch.object(print_lineage, "note_sliced_with", lambda path, said: noted.update(path=path, said=said)):
            result = self._slice(tmp_path, _slicer_that_writes({}))
        expected = {
            "slicer": "prusaslicer", "name": "PrusaSlicer", "version": "2.9.4",
            "profile": "Invented", "profile_version": "1.2.0", "filament_to_nozzle": [1, 2],
        }
        assert result.sliced_with == expected == result.to_dict()["sliced_with"]
        assert noted == {"path": result.output_path, "said": expected}

    def test_another_slicer_in_hand_is_swapped_for_the_makers(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes(seen), slicer=_orca(), found=_prusa())
        assert seen["cmd"][0] == "/fake/PrusaSlicer" and result.sliced_with["slicer"] == "prusaslicer"

    def test_a_slicer_too_old_slices_for_one_nozzle_and_says_so(self, tmp_path, notes):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_swapping(seen, 1), slicer=_prusa("2.4.0"))
        assert result.success and result.sliced_with is None and result.filament_map is None
        # Exactly as a printer sliced with one nozzle: none of the record's extras or routines.
        assert "example_machine_key" not in seen["settings"] and "toolchange_gcode" not in seen["settings"]
        assert seen["tower"] is False
        assert result.one_nozzle == {
            "filament_swaps": 1,
            "maker_slicer": {"name": "PrusaSlicer", "need": "update", "download": "prusa3d.com/p/prusaslicer"},
        }
        assert result.to_dict()["one_nozzle"] == result.one_nozzle
        assert "Sliced for one nozzle: 1 filament swap. " in result.message
        assert result.message.endswith(
            "Update PrusaSlicer to 2.5.0 or newer (free) and Kiln uses both nozzles on its own: "
            "prusa3d.com/p/prusaslicer."
        )

    def test_a_record_that_names_no_slicer_is_refused_and_nothing_runs(self, tmp_path, notes):
        record = {key: value for key, value in _RECORD.items() if key != "maker_slicer"}
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, slicer=_studio(), found=_studio(), record=record)
        assert not isinstance(refused.value, OneNozzleInstead)
        assert refused.value.code == NEEDS_MAKER_SLICER

    def test_a_missing_makers_slicer_slices_with_the_slicer_in_hand(self, tmp_path, notes):
        record = {**_RECORD, "maker_slicer": {"name": "bambu_studio"}}
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_swapping(seen, 3), record=record)
        assert result.slicer and "toolchange_gcode" not in seen["settings"]
        assert result.one_nozzle["filament_swaps"] == 3
        assert "Install Bambu Studio (free) and Kiln uses both nozzles on its own: bambulab.com/download/studio." in (
            result.message
        )

    def test_the_slicer_is_named_first_then_only_for_many_swaps(self, tmp_path, notes):
        def said(swaps: int) -> bool:
            result = self._slice(tmp_path, _slicer_swapping({}, swaps), slicer=_prusa("2.4.0"))
            assert f"Sliced for one nozzle: {swaps} filament swaps." in result.message
            assert ("Update PrusaSlicer" in result.message) is ("maker_slicer" in result.one_nozzle)
            return "maker_slicer" in result.one_nozzle

        assert said(2) is True
        assert said(2) is False
        assert said(maker_slicer_notes.SWAPS_WORTH_SAYING - 1) is False
        assert said(maker_slicer_notes.SWAPS_WORTH_SAYING) is True
        assert said(3) is False

    def test_both_nozzles_are_said_once_after_a_slice_for_one(self, tmp_path, notes):
        self._slice(tmp_path, _slicer_swapping({}, 1), slicer=_prusa("2.4.0"))
        first = self._slice(tmp_path, _slicer_that_writes({}))
        assert first.sliced_with is not None and first.message.endswith(maker_slicer_notes.BOTH_NOW)
        again = self._slice(tmp_path, _slicer_that_writes({}))
        assert maker_slicer_notes.BOTH_NOW not in again.message

    def test_a_map_cannot_be_kept_on_one_nozzle_so_it_is_refused(self, tmp_path, notes):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, slicer=_prusa("2.4.0"), filament_map=[1, 2])
        said = str(refused.value)
        assert refused.value.code == NEEDS_MAKER_SLICER and "2.4.0" in said
        assert "leave the map out" in said and "for one nozzle" in said
        assert not notes.exists()

    def test_a_plate_printed_in_one_colour_says_so_first_and_names_the_slicer_every_time(
        self, tmp_path, notes, monkeypatch,
    ):
        monkeypatch.setattr(print_lineage, "store_path", lambda: tmp_path / "lineage.json")
        lead = "This will print in one colour: This printer's two-nozzle and multi-colour slicing needs PrusaSlicer 2.5.0 or newer"
        for _ in range(3):
            result = self._slice(tmp_path, _slicer_swapping({}, 0, tools=1), slicer=_prusa("2.4.0"))
            assert result.message.startswith(f"{lead}. ")
            assert "Sliced for one nozzle, with no filament swaps." in result.message
            assert "Update PrusaSlicer to 2.5.0 or newer (free)" in result.message
            assert result.one_nozzle["colours_flattened"] is True and "maker_slicer" in result.one_nozzle
        # The person approving the print is told too, from the sliced file's own record.
        row = maker_slicer_notes.approval_row(Path(result.output_path).name, result.output_path)
        assert row == "one colour: This printer's two-nozzle and multi-colour slicing needs PrusaSlicer 2.5.0 or newer"

    def test_a_plate_that_keeps_its_colours_is_not_said_to_lose_them(self, tmp_path, notes, monkeypatch):
        monkeypatch.setattr(print_lineage, "store_path", lambda: tmp_path / "lineage.json")
        result = self._slice(tmp_path, _slicer_swapping({}, 4), slicer=_prusa("2.4.0"))
        assert "colours_flattened" not in result.one_nozzle and "one colour" not in result.message
        assert maker_slicer_notes.approval_row(Path(result.output_path).name, result.output_path) is None

    def test_on_the_shared_server_nothing_is_kept_and_no_slicer_is_named(self, tmp_path, notes, monkeypatch):
        monkeypatch.setattr(maker_slicer_notes, "_hosted", lambda: True)
        result = self._slice(tmp_path, _slicer_swapping({}, 25), slicer=_prusa("2.4.0"))
        assert result.one_nozzle == {"filament_swaps": 25}
        assert "PrusaSlicer" not in result.message and not notes.exists()

    def test_a_map_that_moves_a_filament_is_refused(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, filament_map=[2, 1])
        assert refused.value.code == "NOZZLE_MAP_FIXED"
        self._slice(tmp_path, _slicer_that_writes({}), filament_map=[1, 2])

    def test_more_filaments_than_nozzles_fitted_is_refused(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, _never_runs, filaments=3)
        assert refused.value.code == "MORE_FILAMENTS_THAN_NOZZLES"
        assert "3 filaments" in str(refused.value)

    def test_one_filament_slices_for_one_nozzle_with_nothing_added(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, _slicer_that_writes(seen), filaments=1)
        assert result.sliced_with is None
        assert "example_machine_key" not in seen["settings"] and "toolchange_gcode" not in seen["settings"]
        assert seen["tower"] is None


class TestThePerLayerRoutine:
    """A printer's own per-layer routine runs after Kiln's own per-layer step
    in the machine preset, never in its place; every other routine of the
    printer's takes the place of the preset's own."""

    @staticmethod
    def _machine(settings: dict[str, str], routines: dict[str, str]) -> dict:
        from kiln.slicer_orca import settings_to_orca_presets

        return settings_to_orca_presets({**_SETTINGS, **settings}, routines=routines).machine

    def test_the_step_comes_first_then_the_routine_once(self):
        machine = self._machine(
            {"layer_gcode": "OWN STEP\\nOWN NOTE", "start_gcode": "OWN START"},
            {"layer_change_gcode": "MAKER LAYER\nMAKER NOTE", "machine_start_gcode": "MAKER START"},
        )
        assert machine["layer_change_gcode"] == "OWN STEP\nOWN NOTE\nMAKER LAYER\nMAKER NOTE"
        assert machine["machine_start_gcode"] == "MAKER START"
        # With no step of its own the routine is written as given, and a
        # routine that already begins with the step is not given it twice.
        assert self._machine({}, {"layer_change_gcode": "MAKER LAYER"})["layer_change_gcode"] == "MAKER LAYER"
        again = self._machine({"layer_gcode": "OWN STEP"}, {"layer_change_gcode": "OWN STEP\nMAKER LAYER"})
        assert again["layer_change_gcode"] == "OWN STEP\nMAKER LAYER"


class TestABambuStudioSliceCarriesTheMakersRoutines:
    """A printer whose maker's slicer keeps its routines in preset files is
    sliced for more than one nozzle with those routines in the machine
    preset the slicer is handed: the print starts, changes filament and ends
    the way its maker's slicer would have it, and the result says which
    copy of the maker's settings they came from."""

    def _slice(self, tmp_path, seen, bundles, record=_PAIR):
        with (
            patch("kiln.slicer.find_slicer", return_value=_studio()),
            patch("kiln.maker_slicer._find", return_value=None),
            patch("kiln.maker_slicer._vendor_files", return_value=bundles),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", _studio_that_writes("; filament_map = 1,2\n", seen)),
        ):
            return slice_file(
                _plate(tmp_path), output_dir=str(tmp_path / "out"), profile=_pair_profile(tmp_path, record),
            )

    def test_the_makers_per_layer_routine_runs_after_kilns_own_per_layer_step(self, tmp_path):
        seen: dict = {}
        self._slice(tmp_path, seen, [_preset_bundle(tmp_path / "app")], record=_PAIR_WITH_LAYER)
        assert seen["machine"]["layer_change_gcode"].split("\n") == [
            slicer_profiles._E_RESET, "LAYER NOTE {layer_num}", "LAYER NOTE TWO",
        ]
        # The routines Kiln writes no step of its own into are the maker's alone.
        assert seen["machine"]["machine_start_gcode"] == "TWO START\nLINE TWO"

    def test_the_machine_preset_carries_the_makers_routines(self, tmp_path):
        seen: dict = {}
        result = self._slice(tmp_path, seen, [_preset_bundle(tmp_path / "app")])
        machine = seen["machine"]
        assert machine["machine_start_gcode"] == "TWO START\nLINE TWO"
        assert machine["change_filament_gcode"] == (
            "M620 S[next_filament_id]A\nT[next_filament_id]\nM621 S[next_filament_id]A"
        )
        assert machine["machine_end_gcode"] == "COMMON END"
        assert (result.sliced_with["profile"], result.sliced_with["profile_version"]) == ("Invented", "1.2.0.0")

    def test_without_the_makers_settings_nothing_is_sliced(self, tmp_path):
        with pytest.raises(NozzleMapError) as refused:
            self._slice(tmp_path, {}, [])
        assert refused.value.code == NEEDS_MAKER_SLICER and "Bambu Studio" in str(refused.value)


class TestTheFilesRecord:
    def test_the_slicer_is_kept_on_the_record_and_answered_with_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(print_lineage, "store_path", lambda: tmp_path / "lineage.json")
        model, gcode = tmp_path / "part.stl", tmp_path / "part.gcode"
        _cube(model, 10.0)
        gcode.write_text("G28\nG1 X1 Y1 E1\n")
        lineage_id = print_lineage.record_slice(str(model), str(gcode))
        assert lineage_id
        said = {"slicer": "prusaslicer", "version": "2.9.4"}
        assert print_lineage.note_sliced_with(str(gcode), said) is True
        assert print_lineage.resolve("part.gcode", local_paths=[str(gcode)])["sliced_with"] == said

    def test_a_file_with_no_record_keeps_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(print_lineage, "store_path", lambda: tmp_path / "lineage.json")
        gcode = tmp_path / "loose.gcode"
        gcode.write_text("G28\n")
        assert print_lineage.note_sliced_with(str(gcode), {"slicer": "x"}) is False


#: An invented one-nozzle machine whose record names the routines its maker's
#: slicer runs within a print.
_ONE = {
    "kind": "single",
    "stock": 1,
    "max": 1,
    "sliced_as": 1,
    "maker_slicer": {
        "name": "bambu_studio",
        "routines": {
            "vendor": "Invented",
            "min_profile_version": "01.02.00.00",
            "preset": "Invented Two {nozzle} nozzle",
            "layer_keys": ["layer_change_gcode", "invented_frame_gcode"],
        },
    },
}


def _one_profile(tmp_path: Path, record: dict = _ONE) -> str:
    """The invented one-nozzle machine's profile, as Kiln writes one for a printer."""
    ini = tmp_path / "invented_one.ini"
    body = slicer_profiles._settings_to_ini(dict(_PAIR_SETTINGS), "Invented One", printer_id="invented_one")
    ini.write_text(slicer_profiles._with_nozzles(body, record))
    return str(ini)


class TestASliceForOneNozzleCarriesThePrintersOwnRoutines:
    """A slice for one nozzle carries the routines its printer's record names
    for a print in Kiln's own package, read by the same reader, when the
    printer's maker's slicer is the one making it; Kiln's own per-layer step
    runs first.  Made by any other slicer it carries none of them, and the
    reply says the print has no time-lapse step and what adds it: the first
    time on a printer, and again when what adds it changes."""

    def _slice(self, tmp_path, *, slicer, found=None, bundles=None, record=_ONE, seen=None):
        seen = {} if seen is None else seen
        run = _studio_that_writes("", seen) if slicer.name == "bambustudio" else _slicer_that_writes(seen)
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer),
            patch("kiln.maker_slicer._find", return_value=found),
            patch(
                "kiln.maker_slicer._vendor_files",
                return_value=[_preset_bundle(tmp_path / "app")] if bundles is None else bundles,
            ),
            patch("kiln.slicer._find_bambu_dialect_slicer", return_value=None),
            patch("kiln.slicer._gcode_output_is_complete", return_value=True),
            patch("kiln.slicer._run_slicer_with_startup_retry", run),
        ):
            return slice_file(
                _plate(tmp_path, 1), output_dir=str(tmp_path / "out"), profile=_one_profile(tmp_path, record),
            )

    def test_the_makers_slicer_making_it_carries_them_after_kilns_own_step(self, tmp_path, notes):
        seen: dict = {}
        result = self._slice(tmp_path, slicer=_studio(), seen=seen)
        machine = seen["machine"]
        assert machine["layer_change_gcode"].split("\n") == [
            slicer_profiles._E_RESET, "LAYER NOTE {layer_num}", "LAYER NOTE TWO",
        ]
        assert machine["invented_frame_gcode"] == "FRAME {layer_num}"
        # Kiln's own package writes the start and the filament changes.
        assert "TWO START" not in str(machine.get("machine_start_gcode"))
        assert "M620" not in str(machine.get("change_filament_gcode"))
        assert (result.sliced_with["profile"], result.sliced_with["profile_version"]) == ("Invented", "1.2.0.0")
        assert result.time_lapse is None and "time-lapse" not in result.message

    def test_another_slicer_making_it_carries_none_and_the_reply_names_the_makers(self, tmp_path, notes):
        seen: dict = {}
        result = self._slice(tmp_path, slicer=_prusa(), found=_studio(), seen=seen)
        assert not any("LAYER NOTE" in str(v) or "FRAME" in str(v) for v in seen["settings"].values())
        assert result.sliced_with is None
        assert result.time_lapse == {
            "in_file": False,
            "maker_slicer": {"name": "Bambu Studio", "need": "use", "path": "/fake/BambuStudio"},
        }
        assert (
            "This print has no time-lapse step: it was sliced with PrusaSlicer. Bambu Studio is installed: "
            "slice with it and Kiln adds the printer's own." in result.message
        )

    def test_it_is_said_once_on_a_printer_and_again_when_what_adds_it_changes(self, tmp_path, notes):
        first = self._slice(tmp_path / "a", slicer=_prusa())
        assert "Install Bambu Studio (free) and slice with it, and Kiln adds the printer's own: " in first.message
        assert first.time_lapse["maker_slicer"]["download"] == "bambulab.com/download/studio"
        again = self._slice(tmp_path / "b", slicer=_prusa())
        assert again.time_lapse == {"in_file": False} and "time-lapse" not in again.message
        installed = self._slice(tmp_path / "c", slicer=_prusa(), found=_studio())
        assert installed.time_lapse["maker_slicer"]["need"] == "use" and "Bambu Studio is installed" in installed.message

    def test_settings_older_than_the_record_accepts_carry_none_and_say_to_update(self, tmp_path, notes):
        seen: dict = {}
        result = self._slice(
            tmp_path, slicer=_studio(), bundles=[_preset_bundle(tmp_path / "old", version="01.01.00.00")], seen=seen,
        )
        assert "invented_frame_gcode" not in seen["machine"] and result.sliced_with is None
        assert result.time_lapse["maker_slicer"]["need"] == "update"
        assert (
            "This print has no time-lapse step: the printer settings in the Bambu Studio installed are version "
            "1.1.0.0, older than the 01.02.00.00 Kiln needs. Update Bambu Studio (free) and Kiln adds the "
            "printer's own: bambulab.com/download/studio." in result.message
        )

    def test_a_record_naming_no_such_routines_changes_nothing(self, tmp_path, notes):
        record = {**_ONE, "maker_slicer": {"name": "bambu_studio"}}
        result = self._slice(tmp_path, slicer=_prusa(), record=record)
        assert result.time_lapse is None and "time-lapse" not in result.message

    def test_on_the_shared_server_the_file_says_it_and_nothing_is_said_or_kept(self, tmp_path, notes, monkeypatch):
        monkeypatch.setattr(maker_slicer_notes, "_hosted", lambda: True)
        result = self._slice(tmp_path, slicer=_prusa())
        assert result.time_lapse == {"in_file": False} and "time-lapse" not in result.message
        assert not notes.exists()
