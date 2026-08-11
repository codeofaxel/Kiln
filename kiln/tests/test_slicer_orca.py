"""Tests for kiln.slicer_orca — the OrcaSlicer/BambuStudio preset serializer.

Two halves, and the second is the one that would have caught the bugs.

The unit half pins the translation: which key becomes which, which values Orca
wants as a per-extruder list, and the four invariants that are each a silent
failure — a preset with ``inherits`` left in, ``from`` set to anything but
``system``, a ``compatible_printers`` that does not name the machine, and an
unstated ``use_relative_e_distances``.  Every one of those produces a refusal
or a wrong slice rather than an exception, so nothing but an assertion catches
them.

The binary half drives the real slicer, because this whole feature is a claim
about a subprocess and a mock cannot test a claim about a subprocess.  A fake
``subprocess.run`` that writes ``plate_1.gcode`` and returns 0 passes whether
or not the argv would slice anything at all.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln.slicer import SlicerError, slice_file, slicer_cli_family
from kiln.slicer_orca import (
    ini_to_settings,
    settings_to_orca_presets,
    write_orca_presets,
)
from kiln.slicer_profiles import get_slicer_profile, resolve_slicer_profile

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
_PRUSA = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    """A stand-in for subprocess.CompletedProcess, for the failure shapes."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _write_cube(path: str, size: float = 20.0, *, centre: bool = True) -> str:
    """A binary-STL cube, centred on the origin the way Kiln's meshes are.

    Centring is the default deliberately.  Kiln generates models around the
    origin, and a slicer told not to arrange them puts them half off the bed —
    so a test cube sitting conveniently in the middle of the plate would hide
    exactly the failure a user meets first.
    """
    import struct

    lo = -size / 2 if centre else 0.0
    hi = lo + size
    v = [
        (lo, lo, 0), (hi, lo, 0), (hi, hi, 0), (lo, hi, 0),
        (lo, lo, size), (hi, lo, size), (hi, hi, size), (lo, hi, size),
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
    return path


# ---------------------------------------------------------------------------
# The translation
# ---------------------------------------------------------------------------


class TestKeyTranslation:
    """PrusaSlicer's vocabulary into Orca's."""

    def test_process_keys_are_renamed(self):
        presets = settings_to_orca_presets(
            {
                "layer_height": "0.2",
                "first_layer_height": "0.28",
                "perimeters": "3",
                "fill_density": "20%",
                "fill_pattern": "gyroid",
            }
        )
        assert presets.process["layer_height"] == "0.2"
        assert presets.process["initial_layer_print_height"] == "0.28"
        assert presets.process["wall_loops"] == "3"
        assert presets.process["sparse_infill_density"] == "20%"
        assert presets.process["sparse_infill_pattern"] == "gyroid"
        # The PrusaSlicer spellings must not survive into the preset.
        assert "perimeters" not in presets.process
        assert "fill_density" not in presets.process

    def test_per_extruder_values_become_lists(self):
        """Orca stores these per extruder; a bare scalar is rejected."""
        presets = settings_to_orca_presets(
            {"nozzle_diameter": "0.4", "retract_length": "5.0", "temperature": "200"}
        )
        assert presets.machine["nozzle_diameter"] == ["0.4"]
        assert presets.machine["retraction_length"] == ["5.0"]
        assert presets.filament["nozzle_temperature"] == ["200"]

    def test_bed_shape_becomes_a_corner_list(self):
        presets = settings_to_orca_presets({"bed_shape": "0x0,220x0,220x220,0x220"})
        assert presets.machine["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]

    def test_one_bed_temperature_reaches_every_plate_type(self):
        """Orca keeps a temperature per plate; PrusaSlicer has one bed.

        Setting only one plate would leave the print cold on whichever plate
        the user actually has selected.
        """
        presets = settings_to_orca_presets(
            {"bed_temperature": "60", "first_layer_bed_temperature": "65"}
        )
        for plate in ("cool_plate", "eng_plate", "hot_plate", "textured_plate"):
            assert presets.filament[f"{plate}_temp"] == ["60"]
            assert presets.filament[f"{plate}_temp_initial_layer"] == ["65"]

    def test_gcode_newlines_are_unescaped(self):
        r"""INI carries ``\n`` as two characters; JSON needs the real thing.

        Left escaped, the machine's start G-code arrives as one line with a
        literal backslash-n in it.
        """
        presets = settings_to_orca_presets({"start_gcode": "G28\\nG1 Z5\\nM104 S200"})
        assert presets.machine["machine_start_gcode"] == "G28\nG1 Z5\nM104 S200"
        assert "\\n" not in presets.machine["machine_start_gcode"]


class TestPresetInvariants:
    """The four rules that fail silently when broken.

    Each was measured against OrcaSlicer 2.3.2 by breaking it on purpose and
    watching the slice refuse.
    """

    def test_presets_declare_themselves_system(self):
        """``from: "User"`` makes every process incompatible, whatever it says."""
        presets = settings_to_orca_presets({"layer_height": "0.2"})
        assert presets.machine["from"] == "system"
        assert presets.process["from"] == "system"
        assert presets.filament["from"] == "system"

    def test_process_and_filament_name_the_machine(self):
        """Compatibility is matched on the machine's ``name``, not its filename."""
        presets = settings_to_orca_presets({"layer_height": "0.2"}, name="ender3")
        machine_name = presets.machine["name"]
        assert presets.process["compatible_printers"] == [machine_name]
        assert presets.filament["compatible_printers"] == [machine_name]

    def test_no_preset_carries_inherits(self):
        """Orca does not resolve ``inherits`` for a preset given by path."""
        presets = settings_to_orca_presets({"layer_height": "0.2"})
        for body in (presets.machine, presets.process, presets.filament):
            assert "inherits" not in body

    def test_relative_e_is_always_stated(self):
        """The two slicers default it oppositely, so silence means two things.

        PrusaSlicer defaults to absolute extrusion, Orca to relative.  A
        profile that never mentions the key slices correctly in one and is
        refused by the other for lacking a per-layer reset it never needed.
        """
        absolute = settings_to_orca_presets({"layer_height": "0.2"})
        assert absolute.machine["use_relative_e_distances"] == "0"

        relative = settings_to_orca_presets(
            {"layer_height": "0.2", "use_relative_e_distances": "1"}
        )
        assert relative.machine["use_relative_e_distances"] == "1"

    def test_layer_e_reset_survives_translation(self):
        """A relative-E profile's ``G92 E0`` must land in Orca's layer hook."""
        presets = settings_to_orca_presets(
            {"use_relative_e_distances": "1", "layer_gcode": "G92 E0"}
        )
        assert "G92 E0" in presets.machine["layer_change_gcode"]


class TestIniRoundTrip:
    """Reading back the ini every slicing door already produces."""

    def test_reads_a_real_bundled_profile(self, tmp_path):
        ini = resolve_slicer_profile("ender3")
        settings = ini_to_settings(ini)
        bundled = get_slicer_profile("ender3").settings
        assert settings["layer_height"] == bundled["layer_height"]
        assert settings["nozzle_diameter"] == bundled["nozzle_diameter"]

    def test_gcode_values_with_semicolons_survive(self, tmp_path):
        """A ``;`` is a G-code comment, not an ini comment — and ``%`` is not
        interpolation.  Both are why this is not configparser."""
        ini = tmp_path / "p.ini"
        ini.write_text(
            "# Kiln auto-generated profile: test\n"
            "start_gcode = G28 ; home all\\nM104 S200\n"
            "fill_density = 20%\n"
        )
        settings = ini_to_settings(str(ini))
        assert settings["start_gcode"] == "G28 ; home all\\nM104 S200"
        assert settings["fill_density"] == "20%"

    def test_written_presets_are_valid_json_on_disk(self, tmp_path):
        settings = ini_to_settings(resolve_slicer_profile("ender3"))
        presets = write_orca_presets(settings, str(tmp_path), name="ender3")
        for path in (presets.machine_path, presets.process_path, presets.filament_path):
            assert os.path.isfile(path)
            with open(path) as fh:
                assert isinstance(json.load(fh), dict)


# ---------------------------------------------------------------------------
# The real binary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _installed(_ORCA), reason="needs a real OrcaSlicer")
class TestOrcaActuallySlices:
    """Drive OrcaSlicer for real.

    Nothing above this line can tell a correct command line from one the
    slicer rejects: a mocked ``subprocess.run`` returns 0 and writes a file
    whatever argv it was handed.  These are slow (a few seconds each) and they
    are the only tests here that would notice the feature not working.
    """

    def test_a_bundled_profile_slices_through_slice_file(self, tmp_path):
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("ender3"),
            slicer_path=_ORCA,
        )
        assert result.success is True
        assert os.path.getsize(result.output_path) > 10_000
        with open(result.output_path, encoding="utf-8", errors="replace") as fh:
            gcode = fh.read()
        assert "G1" in gcode

    def test_the_profile_reaches_the_gcode(self, tmp_path):
        """A slice that ignores the profile is worse than one that refuses."""
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("ender3"),
            slicer_path=_ORCA,
        )
        with open(result.output_path, encoding="utf-8", errors="replace") as fh:
            gcode = fh.read()
        bundled = get_slicer_profile("ender3").settings
        assert f"; layer_height = {bundled['layer_height']}" in gcode
        assert f"; nozzle_temperature = {bundled['temperature']}" in gcode
        assert f"; wall_loops = {bundled['perimeters']}" in gcode

    def test_an_origin_centred_model_is_not_left_off_the_plate(self, tmp_path):
        """The model Kiln generates sits at the origin, not at plate centre.

        Orca's plate origin is a corner, so a model left where Kiln put it
        hangs half off the bed and the slicer refuses with "no object is fully
        inside the print volume".  Whatever this backend does about arranging,
        the cube Kiln actually produces has to come out the other side.
        """
        stl = _write_cube(str(tmp_path / "cube.stl"), centre=True)
        result = slice_file(
            stl,
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("ender3"),
            slicer_path=_ORCA,
        )
        assert result.success is True

    @pytest.mark.parametrize("printer_id", ["ender3", "k1", "voron_2"])
    def test_marlin_and_klipper_profiles_both_slice(self, tmp_path, printer_id):
        """Half the bundled profiles are klipper flavour, half marlin."""
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            output_dir=str(tmp_path / printer_id),
            profile=resolve_slicer_profile(printer_id),
            slicer_path=_ORCA,
        )
        assert result.success is True

    def test_output_lands_at_the_path_the_caller_asked_for(self, tmp_path):
        """Orca names its own file; the caller's name has to win."""
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            output_dir=str(tmp_path / "out"),
            output_name="chosen.gcode",
            profile=resolve_slicer_profile("ender3"),
            slicer_path=_ORCA,
        )
        assert result.output_path == str(tmp_path / "out" / "chosen.gcode")
        assert os.path.isfile(result.output_path)
        assert not list((tmp_path / "out").glob("plate_*.gcode"))

    def test_the_binary_is_recognised_as_the_other_dialect(self):
        from kiln.slicer import find_slicer

        assert slicer_cli_family(find_slicer(_ORCA)) == "bambu"

    def test_a_bare_call_with_no_profile_still_slices(self, tmp_path):
        """``slice_file("model.stl")`` is the documented form and must work.

        PrusaSlicer slices on its own defaults when given no profile.  Orca's
        defaults use relative extrusion with no per-layer reset, so the bare
        call failed validation before it started until the generic profile
        was wired in as the answer to "you didn't say".
        """
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(stl, output_dir=str(tmp_path / "out"), slicer_path=_ORCA)
        assert result.success is True
        assert os.path.getsize(result.output_path) > 10_000

    def test_estimates_survive_the_trailing_config_block(self, tmp_path):
        """The numbers a user is shown have to come back, not just the file.

        Orca writes its estimate comments ~585 lines from EOF, behind a config
        block longer than PrusaSlicer's.  A parser reading a fixed tail window
        returned an empty dict for every Orca slice while the values sat in
        the file — a slice that worked and an estimate that silently didn't.
        """
        from kiln.slicer import estimate_print

        stl = _write_cube(str(tmp_path / "cube.stl"))
        estimates = estimate_print(
            stl, profile=resolve_slicer_profile("ender3"), slicer_path=_ORCA
        )
        assert estimates.get("estimated_time_seconds", 0) > 0
        assert estimates.get("filament_length_mm", 0) > 0
        assert estimates.get("layer_count", 0) > 0


class TestMultiPlateAndSalvage:
    """Two outcomes that must not be reported as an ordinary success."""

    def _fake_orca(self, tmp_path):
        from kiln.slicer import SlicerInfo

        return SlicerInfo(path="/x/OrcaSlicer", name="orcaslicer", version="OrcaSlicer-2.3.2:")

    def test_a_model_needing_two_plates_is_refused_not_halved(self, tmp_path):
        """Returning plate 1 of 2 is how a user prints half a job.

        ``--slice 0`` asks for every plate, so a model that does not fit on
        one produces several files while ``slice_file`` can hand back exactly
        one path.
        """
        stl = _write_cube(str(tmp_path / "cube.stl"))

        def fake_run(cmd, **kwargs):
            work = cmd[cmd.index("--outputdir") + 1]
            for n in (1, 2):
                with open(os.path.join(work, f"plate_{n}.gcode"), "w") as fh:
                    fh.write("G28\n; filament used [mm] = 1\n")
            return _completed(0)

        with patch("kiln.slicer.find_slicer", return_value=self._fake_orca(tmp_path)), \
             patch("kiln.slicer.subprocess.run", side_effect=fake_run), \
             pytest.raises(SlicerError, match="2 build plates"):
            slice_file(stl, output_dir=str(tmp_path / "out"))

    def test_a_crash_after_a_complete_write_keeps_the_gcode(self, tmp_path):
        """Parity with the Slic3r path, which salvages exactly this.

        A slicer that dies on exit having already written complete G-code has
        done the work.  Without this the file would be discarded and the user
        told about a Bambu crash that did not happen to them.
        """
        stl = _write_cube(str(tmp_path / "cube.stl"))

        def fake_run(cmd, **kwargs):
            work = cmd[cmd.index("--outputdir") + 1]
            with open(os.path.join(work, "plate_1.gcode"), "w") as fh:
                fh.write("G28\nG1 X1\n; filament used [mm] = 1234\n")
            return _completed(-11)

        with patch("kiln.slicer.find_slicer", return_value=self._fake_orca(tmp_path)), \
             patch("kiln.slicer.subprocess.run", side_effect=fake_run):
            result = slice_file(stl, output_dir=str(tmp_path / "out"))

        assert result.success is True
        assert "crashed on exit" in result.message
        assert os.path.isfile(result.output_path)

    def test_a_crash_with_no_output_is_still_a_failure(self, tmp_path):
        """The salvage must not turn every crash into a success."""
        stl = _write_cube(str(tmp_path / "cube.stl"))

        with patch("kiln.slicer.find_slicer", return_value=self._fake_orca(tmp_path)), \
             patch("kiln.slicer.subprocess.run", return_value=_completed(-11)), \
             pytest.raises(SlicerError, match="crashed while slicing"):
            slice_file(stl, output_dir=str(tmp_path / "out"))


@pytest.mark.skipif(not _installed(_PRUSA), reason="needs a real PrusaSlicer")
class TestPrusaSlicerIsUnaffected:
    """The path that already worked must be untouched by all of this."""

    def test_bundled_profile_still_slices_with_prusaslicer(self, tmp_path):
        stl = _write_cube(str(tmp_path / "cube.stl"))
        result = slice_file(
            stl,
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("ender3"),
            slicer_path=_PRUSA,
        )
        assert result.success is True
        assert os.path.getsize(result.output_path) > 10_000

    def test_prusaslicer_never_sees_an_orca_flag(self, tmp_path):
        """Guards the dispatch: the two dialects share no flags."""
        stl = _write_cube(str(tmp_path / "cube.stl"))
        real_run = subprocess.run
        # Every command, not the first: find_slicer probes --version before the
        # slice, so recording only the first call captures the probe and
        # asserts nothing about the argv under test.
        seen: list[list[str]] = []

        def spy(cmd, **kwargs):
            seen.append(list(cmd))
            return real_run(cmd, **kwargs)

        import kiln.slicer as slicer_module

        original = slicer_module.subprocess.run
        slicer_module.subprocess.run = spy
        try:
            slice_file(
                stl,
                output_dir=str(tmp_path / "out"),
                profile=resolve_slicer_profile("ender3"),
                slicer_path=_PRUSA,
            )
        finally:
            slicer_module.subprocess.run = original

        slice_cmds = [c for c in seen if "--version" not in c]
        assert len(slice_cmds) == 1, f"expected one slice command, got {seen}"
        cmd = slice_cmds[0]
        assert "--export-gcode" in cmd
        assert not {"--slice", "--outputdir", "--load-settings"} & set(cmd)
