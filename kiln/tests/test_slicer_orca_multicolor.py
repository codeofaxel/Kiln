"""Multicolor 3MF slicing through the Orca dialect.

The bug this pins: a painted multicolor 3MF (dual-channel paint_color +
mmu_segmentation, as written by compose_painted_3mf / paint_mesh_regions)
sliced through ANY backend flattened to one filament.  The Orca dialect
spoke the right CLI but ``write_orca_presets`` always emitted exactly one
filament preset, so Orca had no slots for colors 2..N — measured 0 tool
changes on a three-color file that now measures 186.

Three layers of coverage, mirroring test_slicer_orca.py's split:

* unit — the N-filament preset expansion and the process keys the
  painted path needs (each one a measured hard failure when missing);
* wiring — ``_slice_with_orca`` argv and the Prusa→Orca auto-switch,
  with the slicer subprocess mocked (house idiom);
* binary — the real OrcaSlicer slicing a real painted file, asserting
  tool changes in the emitted G-code, because the whole feature is a
  claim about a subprocess.
"""

from __future__ import annotations

import os
import re
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln.multicolor_3mf import (
    compose_painted_3mf,
    detect_3mf_multicolor,
    multicolor_filament_colors,
)
from kiln.slicer import (
    SlicerInfo,
    _wipe_tower_position,
    profile_filament_slots,
    slice_file,
)
from kiln.slicer_orca import (
    PRIME_TOWER_WIDTH_MM,
    settings_to_orca_presets,
    write_orca_presets,
)

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"

_SETTINGS = {
    "nozzle_diameter": "0.4",
    "layer_height": "0.2",
    "bed_shape": "0x0,256x0,256x256,0x256",
    "temperature": "220",
    "bed_temperature": "60",
    "use_relative_e_distances": "1",
}

_COLORS = ["#F2F2F2", "#D32F2F", "#1A1A1A"]

# A tetrahedron tall enough to slice into real layers, with one color per
# side face — every layer touches all three states, so a slice that keeps
# the colors MUST change tools.  Real geometry, not a token fixture: a
# degenerate mesh here would let the regression test pass on a slicer
# that emitted nothing.
_TETRA_TRIS = [
    ((0, 0, 0), (30, 0, 0), (15, 30, 0)),
    ((0, 0, 0), (15, 30, 0), (15, 12, 24)),
    ((30, 0, 0), (15, 12, 24), (15, 30, 0)),
    ((0, 0, 0), (15, 12, 24), (30, 0, 0)),
]
_TETRA_COLORS = [_COLORS[0], _COLORS[0], _COLORS[1], _COLORS[2]]


def _painted_3mf(tmp_path: Path) -> str:
    result = compose_painted_3mf(
        _TETRA_TRIS,
        _TETRA_COLORS,
        output_path=str(tmp_path / "painted.3mf"),
        name="tetra",
    )
    assert result["success"], result
    return result["output_path"]


def _write_cube_stl(path: Path, size: float = 20.0) -> str:
    lo, hi = 0.0, size
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
    return str(path)


def _profile_ini(tmp_path: Path) -> str:
    ini = tmp_path / "profile.ini"
    ini.write_text(
        "".join(f"{k} = {v}\n" for k, v in _SETTINGS.items())
    )
    return str(ini)


def _count_tools(gcode_path: str) -> tuple[list[str], int]:
    text = Path(gcode_path).read_text(errors="replace")
    tools = re.findall(r"^T(\d+)\b", text, re.M)
    changes = sum(1 for a, b in zip(tools, tools[1:], strict=False) if a != b)
    return sorted(set(tools)), changes


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


# ---------------------------------------------------------------------------
# Unit: preset expansion
# ---------------------------------------------------------------------------


class TestFilamentExpansion:
    def test_one_preset_per_color_in_order(self):
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=_COLORS,
        )
        assert [f["filament_colour"] for f in p.filaments] == [
            ["#F2F2F2"], ["#D32F2F"], ["#1A1A1A"],
        ]
        assert [f["name"] for f in p.filaments] == [
            "t_filament_1", "t_filament_2", "t_filament_3",
        ]

    def test_every_slot_states_filament_is_support(self):
        """Measured refusal without it: "filament_is_support's count 1
        not equal to filament_colour's size 3"."""
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=_COLORS,
        )
        for f in p.filaments:
            assert f["filament_is_support"] == ["0"]
            assert len(f["filament_colour"]) == 1

    def test_slots_differ_only_in_identity_and_color(self):
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=_COLORS,
        )
        varying = {"name", "filament_colour"}
        base = {k: v for k, v in p.filaments[0].items() if k not in varying}
        for f in p.filaments[1:]:
            assert {k: v for k, v in f.items() if k not in varying} == base

    def test_line_widths_derive_from_nozzle(self):
        """Measured crash without them: "Flow::spacing() produced
        negative spacing" — an MMU-only flow defaults to zero width."""
        p = settings_to_orca_presets(
            dict(_SETTINGS, nozzle_diameter="0.6"),
            name="t",
            filament_colors=_COLORS,
        )
        for key in (
            "line_width", "inner_wall_line_width", "outer_wall_line_width",
            "top_surface_line_width", "sparse_infill_line_width",
            "internal_solid_infill_line_width", "initial_layer_line_width",
            "support_line_width",
        ):
            assert p.process[key] == "0.63", key

    def test_prime_tower_enabled_and_placed(self):
        """Measured failure without placement: "found gcode in
        unprintable area ... error_code = 4"."""
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=_COLORS,
            wipe_tower_xy=(200.0, 60.0),
        )
        assert p.process["enable_prime_tower"] == "1"
        assert p.process["wipe_tower_x"] == "200.00"
        assert p.process["wipe_tower_y"] == "60.00"
        assert float(p.process["prime_tower_width"]) == PRIME_TOWER_WIDTH_MM

    def test_no_placement_means_no_tower_not_a_broken_one(self):
        """A tower enabled without a placement is what fails the slice
        (Orca's default spot can be off-plate).  Measured: with no tower
        at all the file still slices with every color — so an unknown bed
        costs the tower, never the colors."""
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=_COLORS, wipe_tower_xy=None,
        )
        assert "enable_prime_tower" not in p.process
        assert "wipe_tower_x" not in p.process
        # The rest of the multicolor emission is untouched.
        assert len(p.filaments) == 3
        assert p.process["line_width"] == "0.42"

    def test_single_filament_output_is_unchanged(self):
        """The regression guard: an unpainted slice emits exactly what it
        did before multicolor existed — no prime tower, no widths, one
        filament preset under the historical name."""
        p = settings_to_orca_presets(_SETTINGS, name="t")
        assert len(p.filaments) == 1
        assert p.filaments[0] is p.filament
        assert p.filament["name"] == "t_filament"
        for key in ("enable_prime_tower", "line_width", "wipe_tower_x"):
            assert key not in p.process
        assert "filament_colour" not in p.filament

    def test_one_color_is_not_multicolor(self):
        p = settings_to_orca_presets(
            _SETTINGS, name="t", filament_colors=["#FF0000"],
        )
        assert len(p.filaments) == 1
        assert "enable_prime_tower" not in p.process

    def test_written_files_one_per_slot(self, tmp_path):
        w = write_orca_presets(
            _SETTINGS, str(tmp_path), name="t", filament_colors=_COLORS,
        )
        assert len(w.filament_paths) == 3
        assert all(os.path.isfile(p) for p in w.filament_paths)
        assert w.filament_path == w.filament_paths[0]

    def test_single_written_file_keeps_historical_name(self, tmp_path):
        w = write_orca_presets(_SETTINGS, str(tmp_path), name="t")
        assert w.filament_paths == (w.filament_path,)
        assert os.path.basename(w.filament_path) == "t_filament.json"


# ---------------------------------------------------------------------------
# Unit: detection evidence for the engine
# ---------------------------------------------------------------------------


class TestDetectionForSlicing:
    def test_painted_file_reports_palette_and_slots(self, tmp_path):
        evidence = detect_3mf_multicolor(_painted_3mf(tmp_path))
        assert evidence is not None
        assert evidence["palette"] == _COLORS
        assert evidence["filament_slots_needed"] == 3

    def test_palette_is_the_richest_across_model_members(self, tmp_path):
        """A 3MF may carry several .model parts; the palette count must
        be the MAX over them, not whichever one was read first."""
        path = tmp_path / "two_members.3mf"
        def group(n: int) -> str:
            entries = "".join(
                f'<m:color color="#{i:02X}0000"/>' for i in range(n)
            )
            return (
                f'<model><resources><m:colorgroup id="9">{entries}'
                "</m:colorgroup></resources></model>"
            )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/a.model", group(3))
            zf.writestr("3D/b.model", group(5))
        evidence = detect_3mf_multicolor(str(path))
        assert evidence is not None
        assert evidence["palette_colors"] == 5
        assert evidence["filament_slots_needed"] == 5

    @pytest.mark.parametrize(
        "entries",
        [
            '<m:color color="FF0000"/><m:color color="00FF00"/>',
            "<m:color color='#FF0000'/><m:color color='#00FF00'/>",
            '<m:color id="1" color="#FF0000"/><m:color id="2" color="#00FF00"/>',
        ],
        ids=["no-hash", "single-quoted", "attrs-before-color"],
    )
    def test_palette_spellings_still_detect(self, tmp_path, entries):
        """Detection must not narrow with the value-capturing regex: a
        palette entry spelled unusually is still a palette entry, and
        missing it would silently un-detect a multicolor file."""
        path = tmp_path / "spelling.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "3D/3dmodel.model",
                f'<model><resources><m:colorgroup id="2">{entries}'
                "</m:colorgroup></resources></model>",
            )
        evidence = detect_3mf_multicolor(str(path))
        assert evidence is not None
        assert evidence["palette_colors"] == 2
        assert evidence["palette"] == ["#FF0000", "#00FF00"]

    def test_slots_are_a_max_not_a_count(self, tmp_path):
        """Paint states {1, 3} use two filaments but need THREE slots —
        state 3 is "filament 3", and T2 cannot exist with two presets."""
        path = tmp_path / "sparse.3mf"
        model = (
            '<?xml version="1.0"?><model><resources><object id="1">'
            '<mesh><triangles>'
            '<triangle v1="0" v2="1" v3="2" paint_color="4"/>'
            '<triangle v1="0" v2="2" v3="3" paint_color="0C"/>'
            "</triangles></mesh></object></resources></model>"
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", model)
        evidence = detect_3mf_multicolor(str(path))
        assert evidence is not None
        assert evidence["filament_slots_needed"] == 3

    def test_colors_fall_back_past_the_palette(self, tmp_path):
        evidence = detect_3mf_multicolor(_painted_3mf(tmp_path))
        colors = multicolor_filament_colors(evidence, 5)
        assert colors[:3] == _COLORS
        assert len(colors) == 5
        assert all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in colors[3:])


# ---------------------------------------------------------------------------
# Unit: prime tower placement
# ---------------------------------------------------------------------------


class TestWipeTowerPosition:
    def test_tower_lands_on_bed_and_clear_of_model(self, tmp_path):
        threemf = _painted_3mf(tmp_path)
        xy = _wipe_tower_position(_SETTINGS, threemf, 30.0)
        assert xy is not None
        x, y = xy
        assert x >= 0 and x + 30 <= 256
        assert y >= 0 and y + 30 <= 256
        # The tetra bbox is placed mid-plate by compose_painted_3mf;
        # whichever side won, the tower's footprint must not intersect it.
        from kiln.printers.bed_fit import compute_3mf_geometry_bbox

        bbox = compute_3mf_geometry_bbox(threemf)
        assert bbox is not None
        assert (
            x >= bbox["x_max"] or x + 30 <= bbox["x_min"]
            or y >= bbox["y_max"] or y + 30 <= bbox["y_min"]
        )

    def test_no_bed_shape_means_no_position(self, tmp_path):
        assert _wipe_tower_position({}, _painted_3mf(tmp_path), 30.0) is None

    def test_bed_too_small_gives_no_position_not_an_off_plate_one(
        self, tmp_path,
    ):
        """The corner formula on a bed narrower than the tower yields a
        NEGATIVE coordinate — an off-plate tower, which is exactly the
        "unprintable area" failure this placement exists to avoid.  No
        position means no tower, and the colors still print."""
        xy = _wipe_tower_position(
            {"bed_shape": "0x0,30x0,30x30,0x30"},
            _painted_3mf(tmp_path),
            30.0,
        )
        assert xy is None

    def test_bed_exactly_large_enough_still_places(self, tmp_path):
        xy = _wipe_tower_position(
            {"bed_shape": "0x0,40x0,40x40,0x40"},
            _painted_3mf(tmp_path),
            30.0,
        )
        assert xy is not None
        x, y = xy
        assert x >= 0
        assert y >= 0
        assert x + 30 <= 40
        assert y + 30 <= 40

    def test_unreadable_model_still_lands_on_bed(self, tmp_path):
        missing = str(tmp_path / "nope.3mf")
        xy = _wipe_tower_position(_SETTINGS, missing, 30.0)
        assert xy is not None
        x, y = xy
        assert x >= 0 and x + 30 <= 256
        assert y >= 0 and y + 30 <= 256


# ---------------------------------------------------------------------------
# Wiring: argv and the auto-switch (mocked slicer — house idiom)
# ---------------------------------------------------------------------------


def _orca_info() -> SlicerInfo:
    return SlicerInfo(path="/fake/OrcaSlicer", name="orcaslicer",
                      version="OrcaSlicer-2.3.2:")


def _prusa_info() -> SlicerInfo:
    return SlicerInfo(path="/fake/PrusaSlicer", name="prusa-slicer",
                      version="PrusaSlicer-2.9.4 based on Slic3r")


class TestOrcaArgvWiring:
    def test_load_filaments_gets_all_slots(self, tmp_path):
        threemf = _painted_3mf(tmp_path)
        seen: dict = {}

        def fake_run(cmd, *, timeout, retryable_returncodes,
                     remove_partial_output, output_is_complete):
            seen["cmd"] = list(cmd)
            work_dir = cmd[cmd.index("--outputdir") + 1]
            Path(work_dir, "plate_1.gcode").write_text(
                "T0\nT1\nT2\n; filament used [mm] = 1.0\n"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("kiln.slicer.find_slicer", return_value=_orca_info()),
            patch("kiln.slicer._run_slicer_with_startup_retry", fake_run),
        ):
            result = slice_file(
                threemf,
                output_dir=str(tmp_path / "out"),
                profile=_profile_ini(tmp_path),
            )
        assert result.success
        cmd = seen["cmd"]
        filaments = cmd[cmd.index("--load-filaments") + 1].split(";")
        assert len(filaments) == 3
        assert [os.path.basename(f) for f in filaments] == [
            "profile_filament_1.json",
            "profile_filament_2.json",
            "profile_filament_3.json",
        ]

    def test_plain_3mf_still_loads_one_filament(self, tmp_path):
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        cube = _write_cube_stl(tmp_path / "cube.stl")
        composed = compose_multicolor_3mf(
            [ColorPart(cube, extruder=1, name="cube")],
            output_path=str(tmp_path / "plain.3mf"),
        )
        assert composed["success"], composed
        seen: dict = {}

        def fake_run(cmd, *, timeout, retryable_returncodes,
                     remove_partial_output, output_is_complete):
            seen["cmd"] = list(cmd)
            work_dir = cmd[cmd.index("--outputdir") + 1]
            Path(work_dir, "plate_1.gcode").write_text(
                "T0\n; filament used [mm] = 1.0\n"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("kiln.slicer.find_slicer", return_value=_orca_info()),
            patch("kiln.slicer._run_slicer_with_startup_retry", fake_run),
        ):
            result = slice_file(
                composed["output_path"],
                output_dir=str(tmp_path / "out"),
                profile=_profile_ini(tmp_path),
            )
        assert result.success
        cmd = seen["cmd"]
        filaments = cmd[cmd.index("--load-filaments") + 1].split(";")
        assert len(filaments) == 1


class TestPrusaAutoSwitch:
    def test_painted_3mf_switches_to_installed_orca(self, tmp_path):
        threemf = _painted_3mf(tmp_path)
        seen: dict = {}

        def fake_orca(slicer, input_abs, out_file, *, profile,
                      extra_args, timeout, multicolor=None):
            seen["slicer"] = slicer
            seen["multicolor"] = multicolor
            from kiln.slicer import SliceResult

            Path(out_file).write_text("T0\n; filament used\n")
            return SliceResult(success=True, output_path=out_file,
                               slicer=slicer.name, message="Sliced")

        with (
            patch("kiln.slicer.find_slicer", return_value=_prusa_info()),
            patch("kiln.slicer._find_bambu_dialect_slicer",
                  return_value=_orca_info()),
            patch("kiln.slicer._slice_with_orca", fake_orca),
        ):
            result = slice_file(
                threemf,
                output_dir=str(tmp_path / "out"),
                profile=_profile_ini(tmp_path),
            )
        assert seen["slicer"].name == "orcaslicer"
        assert result.success
        assert "auto-selected" in result.message
        # The evidence is gathered once and handed on, so the backend
        # never re-scans the archive to learn what slice_file already knew.
        assert seen["multicolor"]["filament_slots_needed"] == 3

    def test_explicit_slicer_path_is_honoured(self, tmp_path):
        """An explicit slicer_path is the user's choice: no switch, even
        for a painted file with an Orca installed."""
        threemf = _painted_3mf(tmp_path)
        fake_prusa = tmp_path / "PrusaSlicer"
        fake_prusa.write_text("#!/bin/sh\necho PrusaSlicer-2.9.4\n")
        fake_prusa.chmod(0o755)

        def fake_run(cmd, *, timeout, retryable_returncodes,
                     remove_partial_output, output_is_complete):
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text("T0\n; filament used [mm] = 1.0\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("kiln.slicer._find_bambu_dialect_slicer") as probe,
            patch("kiln.slicer._run_slicer_with_startup_retry", fake_run),
        ):
            result = slice_file(
                threemf,
                output_dir=str(tmp_path / "out"),
                profile=_profile_ini(tmp_path),
                slicer_path=str(fake_prusa),
            )
        probe.assert_not_called()
        assert result.success

    def test_multi_slot_profile_keeps_prusa(self, tmp_path):
        """A profile that already expresses the colors (MMU) is not
        second-guessed — PrusaSlicer can print them."""
        threemf = _painted_3mf(tmp_path)
        ini = tmp_path / "mmu.ini"
        ini.write_text(
            "nozzle_diameter = 0.4,0.4,0.4\n"
            "bed_shape = 0x0,256x0,256x256,0x256\n"
        )

        def fake_run(cmd, *, timeout, retryable_returncodes,
                     remove_partial_output, output_is_complete):
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text("T0\n; filament used [mm] = 1.0\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("kiln.slicer.find_slicer", return_value=_prusa_info()),
            patch("kiln.slicer._find_bambu_dialect_slicer") as probe,
            patch("kiln.slicer._run_slicer_with_startup_retry", fake_run),
        ):
            result = slice_file(
                threemf,
                output_dir=str(tmp_path / "out"),
                profile=str(ini),
            )
        probe.assert_not_called()
        assert result.success

    def test_no_orca_installed_keeps_prusa(self, tmp_path):
        threemf = _painted_3mf(tmp_path)

        def fake_run(cmd, *, timeout, retryable_returncodes,
                     remove_partial_output, output_is_complete):
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text("T0\n; filament used [mm] = 1.0\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("kiln.slicer.find_slicer", return_value=_prusa_info()),
            patch("kiln.slicer._find_bambu_dialect_slicer",
                  return_value=None),
            patch("kiln.slicer._run_slicer_with_startup_retry", fake_run),
        ):
            result = slice_file(
                threemf,
                output_dir=str(tmp_path / "out"),
                profile=_profile_ini(tmp_path),
            )
        assert result.success
        assert "auto-selected" not in result.message


class TestProfileSlotCount:
    def test_no_profile_is_one(self):
        assert profile_filament_slots(None) == 1

    def test_three_values_are_three(self, tmp_path):
        ini = tmp_path / "p.ini"
        ini.write_text("nozzle_diameter = 0.4, 0.4, 0.6\n")
        assert profile_filament_slots(str(ini)) == 3


# ---------------------------------------------------------------------------
# Binary: the real slicer, the real claim
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
class TestRealOrcaMulticolor:
    """Driven with a BUNDLED profile, the one every slicing door resolves.

    Not the minimal ini the mocked tests use: OrcaSlicer 2.3.2 SIGSEGVs
    on a multicolor slice from a profile that thin (measured — the same
    file slices clean from the bundled bambu_a1 profile), and this class
    exists to test Kiln's real path, not to catalogue Orca's crashes.
    """

    def test_painted_3mf_keeps_all_colors(self, tmp_path):
        """The headline: three painted colors in, three tools out, with
        actual tool changes — the exact measurement the
        multicolor_flattened advisory takes, asserted positively."""
        from kiln.slicer_profiles import resolve_slicer_profile

        result = slice_file(
            _painted_3mf(tmp_path),
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("bambu_a1"),
            slicer_path=_ORCA,
            timeout=600,
        )
        assert result.success, result.message
        tools, changes = _count_tools(result.output_path)
        assert tools == ["0", "1", "2"]
        assert changes > 0

    def test_unpainted_model_still_slices_single_filament(self, tmp_path):
        """Regression: the multicolor machinery must not touch a plain
        single-filament slice."""
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        cube = _write_cube_stl(tmp_path / "cube.stl")
        composed = compose_multicolor_3mf(
            [ColorPart(cube, extruder=1, name="cube")],
            output_path=str(tmp_path / "plain.3mf"),
        )
        assert composed["success"], composed
        from kiln.slicer_profiles import resolve_slicer_profile

        result = slice_file(
            composed["output_path"],
            output_dir=str(tmp_path / "out"),
            profile=resolve_slicer_profile("bambu_a1"),
            slicer_path=_ORCA,
            timeout=600,
        )
        assert result.success, result.message
        tools, _changes = _count_tools(result.output_path)
        assert len(tools) <= 1
