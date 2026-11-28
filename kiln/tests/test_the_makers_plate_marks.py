"""A plate a filament's maker rules out stays ruled out in every slice Kiln makes.

Covers the reader of the maker's own filament settings
(:func:`kiln.maker_slicer.maker_filament`), the plate a print is for
(:func:`kiln.maker_slicer.plate_for_print`), the presets handed to OrcaSlicer
and Bambu Studio (:func:`kiln.slicer_orca.settings_to_orca_presets`), the
refusal before slicing (:func:`kiln.slicer.slice_file`), the print file
(:func:`kiln.printers.bambu_3mf.build_bambu_3mf`) and the upload that wraps it.
With Bambu Studio installed, the same is read from a real slice.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import maker_slicer as ms
from kiln import slicer as kiln_slicer
from kiln.slicer import SlicerInfo, slice_file
from kiln.slicer_orca import settings_to_orca_presets
from kiln.slicer_profiles import resolve_slicer_profile

_P1S = "Bambu Lab P1S"
_STUDIO_BINARY = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _studio_app(root: Path, *, abs_cool: str = "0", default_plate: str = "Textured PEI Plate") -> Path:
    """A slicer laid out the way Bambu Studio keeps its makers' settings,
    holding one printer and three filaments built on a shared template."""
    binary = root / "BambuStudio.app" / "Contents" / "MacOS" / "BambuStudio"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    profiles = root / "BambuStudio.app" / "Contents" / "Resources" / "profiles"
    folder = profiles / "BBL"
    machines = {
        "fdm_machine_common": {"instantiation": "false"},
        "Bambu Lab P1S 0.4 nozzle": {
            "inherits": "fdm_machine_common", "instantiation": "true",
            "printer_model": _P1S, "printer_variant": "0.4",
        },
        "Bambu Lab P1S 0.6 nozzle": {
            "inherits": "fdm_machine_common", "instantiation": "true",
            "printer_model": _P1S, "printer_variant": "0.6",
        },
    }
    filaments = {
        "fdm_filament_common": {
            "instantiation": "false", "filament_type": ["PLA"], "required_nozzle_HRC": ["3"],
            "cool_plate_temp": ["60"], "eng_plate_temp": ["60"], "hot_plate_temp": ["60"],
            "textured_plate_temp": ["60"], "supertack_plate_temp": ["45"],
        },
        "fdm_filament_abs": {
            "inherits": "fdm_filament_common", "instantiation": "false", "filament_type": ["ABS"],
            "cool_plate_temp": [abs_cool], "cool_plate_temp_initial_layer": [abs_cool],
            "eng_plate_temp": ["90"], "hot_plate_temp": ["90"], "textured_plate_temp": ["90"],
            "supertack_plate_temp": ["0"],
        },
        "Generic ABS": {
            "inherits": "fdm_filament_abs", "instantiation": "true",
            "compatible_printers": ["Bambu Lab P1S 0.4 nozzle", "Bambu Lab P1S 0.6 nozzle"],
        },
        "Brand ABS": {
            "inherits": "fdm_filament_abs", "instantiation": "true", "cool_plate_temp": ["70"],
            "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"],
        },
        "Generic PA-CF": {
            "inherits": "fdm_filament_common", "instantiation": "true", "filament_type": ["PA-CF"],
            "required_nozzle_HRC": ["40"], "cool_plate_temp": ["0"],
            "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"],
        },
        "Generic PLA": {
            "inherits": "fdm_filament_common", "instantiation": "true", "eng_plate_temp": ["0"],
            "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"],
        },
    }
    for name, body in machines.items():
        _write(folder / "machine" / f"{name}.json", {"name": name, **body})
    for name, body in filaments.items():
        _write(folder / "filament" / f"{name}.json", {"name": name, **body})
    _write(folder / "machine" / f"{_P1S}.json", {"name": _P1S, "default_bed_type": default_plate})
    _write(profiles / "BBL.json", {
        "name": "Bambu Lab",
        "version": "02.00.00.01",
        "machine_model_list": [{"name": _P1S, "sub_path": f"machine/{_P1S}.json"}],
        "machine_list": [{"name": n, "sub_path": f"machine/{n}.json"} for n in machines],
        "filament_list": [{"name": n, "sub_path": f"filament/{n}.json"} for n in filaments],
    })
    return binary


@pytest.fixture
def studio(tmp_path):
    """The laid-out slicer, found as the installed Bambu Studio."""
    binary = _studio_app(tmp_path)
    info = SlicerInfo(path=str(binary), name="bambustudio")

    def find(slicer_id):
        return info if slicer_id == "bambu_studio" else None

    with patch("kiln.maker_slicer._find", side_effect=find):
        yield info


def _facts(**plates) -> ms.MakerFilament:
    return ms.MakerFilament(
        slicer="bambu_studio", vendor="BBL", preset="Generic ABS", version="2.0", filament_type="ABS",
        plates={plate: (value, value) for plate, value in plates.items()},
    )


class TestTheMakersFilament:
    def test_reads_the_preset_through_what_it_inherits(self, studio):
        facts = ms.maker_filament("ABS", _P1S, nozzle="0.4")
        assert facts is not None
        assert (facts.slicer, facts.vendor, facts.preset) == ("bambu_studio", "BBL", "Generic ABS")
        assert facts.plates["cool_plate"] == (0.0, 0.0)
        assert facts.plates["textured_plate"] == (90.0, None)
        assert facts.unsupported == ("cool_plate", "supertack_plate")
        assert facts.required_nozzle_hrc == 3.0
        assert facts.default_plate == "textured_plate"

    def test_the_makers_generic_preset_is_read_before_a_brand(self, studio):
        assert ms.maker_filament("ABS", _P1S, nozzle="0.4").preset == "Generic ABS"

    def test_a_nozzle_size_picks_the_presets_for_it(self, studio):
        assert ms.maker_filament("PLA", _P1S, nozzle="0.6") is None
        assert ms.maker_filament("PLA", _P1S, nozzle="0.4").unsupported == ("eng_plate",)

    def test_the_nozzle_hardness_the_maker_asks_for(self, studio):
        assert ms.maker_filament("PA-CF", _P1S, nozzle="0.4").required_nozzle_hrc == 40.0

    def test_an_unknown_printer_or_filament_is_not_known(self, studio):
        assert ms.maker_filament("ABS", "Bambu Lab Q9") is None
        assert ms.maker_filament("PEEK", _P1S) is None
        assert ms.maker_filament("", _P1S) is None
        assert ms.maker_filament("ABS", None) is None

    def test_a_printer_name_that_lists_several_is_read_by_its_first(self, studio):
        assert ms.maker_filament("ABS", f"{_P1S} / P1S Combo", nozzle="0.4") is not None

    def test_broken_settings_are_not_known_and_never_raise(self, tmp_path):
        binary = _studio_app(tmp_path)
        (binary.parents[1] / "Resources" / "profiles" / "BBL" / "filament" / "Generic ABS.json").write_text("{")
        info = SlicerInfo(path=str(binary), name="bambustudio")
        with patch("kiln.maker_slicer._find", side_effect=lambda s: info if s == "bambu_studio" else None):
            facts = ms.maker_filament("ABS", _P1S, nozzle="0.4")
        assert facts is None or facts.preset == "Brand ABS"

    def test_no_maker_slicer_installed_is_not_known(self):
        with patch("kiln.maker_slicer._find", return_value=None):
            assert ms.maker_filament("ABS", _P1S) is None


class TestThePlateForAPrint:
    def test_a_named_plate_the_maker_rules_out_is_refused_in_plain_words(self, studio):
        with pytest.raises(ms.PlateRefused) as caught:
            ms.plate_for_print(["ABS"], _P1S, plate="cool_plate", nozzle="0.4")
        assert caught.value.code == ms.FILAMENT_NOT_FOR_PLATE
        said = str(caught.value)
        assert "ABS must not be printed on the Cool Plate" in said
        assert "nothing was sliced" in said
        assert "Textured PEI Plate" in said
        assert "_plate" not in said

    def test_a_named_plate_the_maker_allows_is_kept(self, studio):
        choice = ms.plate_for_print(["ABS"], _P1S, plate="eng_plate", nozzle="0.4")
        assert (choice.plate, choice.stated) == ("eng_plate", True)

    def test_every_filament_of_the_print_is_held_to_the_plate(self, studio):
        with pytest.raises(ms.PlateRefused, match="PLA must not be printed on the Engineering Plate"):
            ms.plate_for_print(["ABS", "PLA"], _P1S, plate="eng_plate", nozzle="0.4")

    def test_an_unnamed_plate_is_the_one_the_makers_slicer_picks(self, studio):
        choice = ms.plate_for_print(["ABS"], _P1S, nozzle="0.4")
        assert (choice.plate, choice.stated) == ("textured_plate", False)
        assert choice.setting_name("bambu_studio") == "Textured PEI Plate"

    def test_an_unnamed_plate_never_lands_where_the_maker_rules_it_out(self, tmp_path):
        binary = _studio_app(tmp_path, default_plate="Cool Plate")
        info = SlicerInfo(path=str(binary), name="bambustudio")
        with patch("kiln.maker_slicer._find", side_effect=lambda s: info if s == "bambu_studio" else None):
            choice = ms.plate_for_print(["ABS"], _P1S, nozzle="0.4")
        assert choice.plate == "textured_plate"

    def test_a_plate_kiln_does_not_know_is_refused(self, studio):
        with pytest.raises(ms.PlateRefused) as caught:
            ms.plate_for_print(["ABS"], _P1S, plate="glass_plate")
        assert caught.value.code == ms.UNKNOWN_PLATE

    def test_with_no_facts_a_named_plate_stands_and_none_is_chosen(self):
        with patch("kiln.maker_slicer._find", return_value=None):
            assert ms.plate_for_print(["ABS"], _P1S, plate="cool_plate").plate == "cool_plate"
            assert ms.plate_for_print(["ABS"], _P1S).plate is None

    def test_a_plate_a_slicer_has_no_name_for_is_told_as_one_it_does(self):
        choice = ms.PlateChoice(plate="supertack_plate", stated=True, makers=(_facts(cool_plate=0.0),))
        assert choice.setting_name("bambu_studio") == "Supertack Plate"
        assert choice.setting_name("orcaslicer") == "Textured PEI Plate"


class TestThePresetsKeepTheMakersMark:
    def _settings(self):
        return {"bed_temperature": "100", "first_layer_bed_temperature": "105", "filament_type": "ABS"}

    def test_a_plate_the_maker_rules_out_keeps_its_zero(self):
        filament = settings_to_orca_presets(
            self._settings(), makers=[_facts(cool_plate=0.0, supertack_plate=0.0, textured_plate=90.0)],
        ).filament
        for plate in ("cool_plate", "supertack_plate"):
            assert filament[f"{plate}_temp"] == ["0"]
            assert filament[f"{plate}_temp_initial_layer"] == ["0"]

    def test_every_other_plate_carries_the_settings_bed_temperature(self):
        filament = settings_to_orca_presets(self._settings(), makers=[_facts(cool_plate=0.0)]).filament
        for plate in ("eng_plate", "hot_plate", "textured_plate"):
            assert filament[f"{plate}_temp"] == ["100"]
            assert filament[f"{plate}_temp_initial_layer"] == ["105"]

    def test_the_slicer_is_told_the_plate(self):
        presets = settings_to_orca_presets(self._settings(), bed_type_name="Textured PEI Plate")
        assert presets.process["curr_bed_type"] == "Textured PEI Plate"
        assert "curr_bed_type" not in settings_to_orca_presets(self._settings()).process

    def test_each_filament_slot_keeps_its_own_makers_marks(self):
        pla = ms.MakerFilament(
            slicer="bambu_studio", vendor="BBL", preset="Generic PLA", version=None, filament_type="PLA",
            plates={"eng_plate": (0.0, 0.0)},
        )
        presets = settings_to_orca_presets(
            {**self._settings(), "filament_type": "ABS;PLA"},
            filament_colors=["#FF0000", "#0000FF"],
            makers=[_facts(cool_plate=0.0), pla],
        )
        first, second = presets.filaments
        assert first["cool_plate_temp"] == ["0"] and first["eng_plate_temp"] == ["100"]
        assert second["eng_plate_temp"] == ["0"] and second["cool_plate_temp"] == ["100"]


def _cube(path: Path) -> str:
    points = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid cube"]
    for face in faces:
        lines += [" facet normal 0 0 0", "  outer loop"]
        lines += [f"   vertex {points[i][0] + 100} {points[i][1] + 100} {points[i][2]}" for i in face]
        lines += ["  endloop", " endfacet"]
    path.write_text("\n".join([*lines, "endsolid cube"]) + "\n", encoding="utf-8")
    return str(path)


def _no_slicer_runs(*_args, **_kwargs):
    raise AssertionError("the slicer ran")


class TestTheSliceIsRefusedBeforeSlicing:
    @pytest.mark.parametrize("name", ["prusa-slicer", "bambustudio"])
    def test_a_plate_the_maker_rules_out_is_refused_whichever_slicer(self, tmp_path, studio, name):
        slicer = SlicerInfo(path=str(tmp_path / name), name=name)
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer),
            patch("kiln.slicer._slice_decided", _no_slicer_runs),
            pytest.raises(kiln_slicer.FilamentNotForPlateError) as caught,
        ):
            slice_file(
                _cube(tmp_path / "cube.stl"), profile=resolve_slicer_profile("bambu_p1s"), material="ABS",
                bed_type="cool_plate", output_dir=str(tmp_path / "out"),
            )
        assert caught.value.code == ms.FILAMENT_NOT_FOR_PLATE
        assert "must not be printed on the Cool Plate" in str(caught.value)

    def test_an_allowed_plate_reaches_the_slicer_with_the_makers_facts(self, tmp_path, studio):
        seen = {}

        def decided(*_args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

        slicer = SlicerInfo(path=str(tmp_path / "bambustudio"), name="bambustudio")
        with (
            patch("kiln.slicer.find_slicer", return_value=slicer),
            patch("kiln.slicer._slice_decided", decided),
            pytest.raises(RuntimeError, match="stop here"),
        ):
            slice_file(
                _cube(tmp_path / "cube.stl"), profile=resolve_slicer_profile("bambu_p1s"), material="ABS",
                output_dir=str(tmp_path / "out"),
            )
        assert seen["plate"].plate == "textured_plate"
        assert seen["plate"].makers[0].unsupported == ("cool_plate", "supertack_plate")


_ABS_BODY = "; filament_type = ABS\n;LAYER_CHANGE\n;Z:0.2\nG1 X10 Y10 E1\n;LAYER_CHANGE\n;Z:0.4\nG1 X20 Y20 E1\n"


class TestThePrintFileIsRefusedForThatPlate:
    def test_the_print_file_is_not_made(self, tmp_path, studio):
        from kiln.printers.bambu_3mf import BambuPrintSettings, build_bambu_3mf

        with pytest.raises(ms.PlateRefused, match="must not be printed on the Cool Plate") as caught:
            build_bambu_3mf(
                _ABS_BODY, str(tmp_path / "out.3mf"),
                settings=BambuPrintSettings(bed_type="cool_plate"), printer_model="bambu_p1s",
            )
        assert caught.value.code == ms.FILAMENT_NOT_FOR_PLATE
        assert not (tmp_path / "out.3mf").exists()

    def test_a_refused_wrap_is_never_uploaded_raw(self, tmp_path):
        from kiln.printers.upload_prep import prepare_upload_for_adapter

        class Wraps:
            def wrap_gcode_as_3mf(self, _path, **_kwargs):
                raise ms.PlateRefused("refused", code=ms.FILAMENT_NOT_FOR_PLATE)

        gcode = tmp_path / "part.gcode"
        gcode.write_text(_ABS_BODY, encoding="utf-8")
        with (
            patch("kiln.printers.upload_prep.adapter_wraps_gcode", return_value=True),
            pytest.raises(ms.PlateRefused),
        ):
            prepare_upload_for_adapter(Wraps(), str(gcode))


class TestTheSlicersOwnRefusal:
    @pytest.mark.parametrize("said", [
        "Plate 1: Cool Plate does not support filament 1",
        "Filaments are not compatible with the plate type. Please verify the slicing of all plates.",
    ])
    def test_is_said_as_a_plate_refusal(self, said):
        from kiln.slicer import _orca_refusal

        refusal = _orca_refusal("BambuStudio", 195, said)
        assert refusal is not None and refusal.code == ms.FILAMENT_NOT_FOR_PLATE
        assert "rule out the plate the print is for" in str(refusal)


_studio_installed = pytest.mark.skipif(
    not (os.path.isfile(_STUDIO_BINARY) or shutil.which("bambu-studio")), reason="Bambu Studio not installed",
)


@_studio_installed
class TestWithBambuStudio:
    """The marks as the installed Bambu Studio reads them, on real slices."""

    def _binary(self) -> str:
        return _STUDIO_BINARY if os.path.isfile(_STUDIO_BINARY) else shutil.which("bambu-studio")

    def test_its_own_settings_rule_abs_off_the_cool_plate(self):
        facts = ms.maker_filament("ABS", _P1S, nozzle="0.4", slicer=SlicerInfo(path=self._binary(), name="bambustudio"))
        assert facts is not None and not facts.supports("cool_plate")

    def test_a_slice_keeps_the_zero_and_names_the_plate(self, tmp_path):
        result = slice_file(
            _cube(tmp_path / "cube.stl"), profile=resolve_slicer_profile("bambu_p1s"), material="ABS",
            slicer_path=self._binary(), output_dir=str(tmp_path / "out"),
        )
        text = Path(result.output_path).read_text(encoding="utf-8", errors="replace")
        assert "; curr_bed_type = Textured PEI Plate" in text
        assert "; cool_plate_temp = 0" in text

    def test_the_slicers_own_check_still_refuses_kilns_presets(self, tmp_path):
        from kiln.slicer import SlicerRefusal, _slice_with_orca

        info = SlicerInfo(path=self._binary(), name="bambustudio")
        facts = ms.maker_filament("ABS", _P1S, nozzle="0.4", slicer=info)
        profile = resolve_slicer_profile("bambu_p1s")
        with pytest.raises(SlicerRefusal) as caught:
            _slice_with_orca(
                info, _cube(tmp_path / "cube.stl"), str(tmp_path / "out.gcode"), profile=profile,
                extra_args=None, timeout=240,
                plate=ms.PlateChoice(plate="cool_plate", stated=True, makers=(facts,)),
            )
        assert caught.value.code == ms.FILAMENT_NOT_FOR_PLATE

