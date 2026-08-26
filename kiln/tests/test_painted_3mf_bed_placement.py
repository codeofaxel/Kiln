"""Painted 3MF bed placement, and legible off-bed slicing failures.

A painted 3MF used to ship with an identity build transform even when its
mesh spanned negative coordinates.  Slicers honour a 3MF's transforms
literally — PrusaSlicer auto-centres loose STL geometry but not a 3MF —
so on a corner-origin bed the whole object sat off the plate.  PrusaSlicer
then printed "All objects are outside of the print volume." to STDERR and
exited 0 without writing gcode, which Kiln surfaced as a generic "Slicer
completed but output file was not created" built from the EMPTY stdout.

Three fixes, three test groups:

* ``compose_painted_3mf`` bakes a centring translation into the build
  item transform whenever the mesh bbox misses the plate.
* ``slice_file`` refuses an off-bed 3MF BEFORE launching the slicer,
  naming the offending bbox and the profile's bed.
* the no-output-file error now carries stderr, where the real reason was.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln.multicolor_3mf import compose_painted_3mf
from kiln.printers.bed_fit import compute_3mf_geometry_bbox
from kiln.slicer import SlicerError, _bed_xy_bounds_from_profile, slice_file

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: A tetra spanning x -30..-20, y -31..-21 — every vertex off a
#: corner-origin bed, the shape of the original repro (threaded_jar
#: template: jar centred at x=0, lid at x=65, bbox x -27.5..96.1).
_NEG_TETRA = [
    ((-30, -31, 0), (-20, -31, 0), (-25, -21, 0)),
    ((-30, -31, 0), (-25, -21, 0), (-25, -26, 8)),
    ((-20, -31, 0), (-25, -26, 8), (-25, -21, 0)),
    ((-30, -31, 0), (-25, -26, 8), (-20, -31, 0)),
]

#: The same tetra already on the plate.
_POS_TETRA = [
    tuple(tuple(c + 100 for c in v) for v in tri) for tri in _NEG_TETRA
]

_COLORS: list[str | None] = ["#ff0000", "#00ff00", None, "#ff0000"]


def _item_translation(threemf_path: str) -> tuple[float, float, float]:
    """The tx/ty/tz of the single build item's transform."""
    with zipfile.ZipFile(threemf_path) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    m = re.search(r'<item objectid="1" transform="([^"]+)"', xml)
    assert m, "build item with transform not found"
    values = [float(v) for v in m.group(1).split()]
    assert len(values) == 12
    assert values[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    return (values[9], values[10], values[11])


def _write_3mf(path: Path, model_xml: str) -> str:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
    return str(path)


def _off_bed_model_xml() -> str:
    """A minimal 3MF model: negative vertices under an identity transform."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources>"
        '<object id="1" type="model"><mesh><vertices>'
        '<vertex x="-30" y="-31" z="0"/>'
        '<vertex x="-20" y="-31" z="0"/>'
        '<vertex x="-25" y="-21" z="8"/>'
        "</vertices><triangles>"
        '<triangle v1="0" v2="1" v3="2"/>'
        "</triangles></mesh></object>"
        "</resources><build>"
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        "</build></model>"
    )


def _bambu_bed_ini(tmp_path: Path) -> str:
    ini = tmp_path / "bed.ini"
    ini.write_text(
        "layer_height = 0.2\n"
        "bed_shape = 0x0,256x0,256x256,0x256\n"
    )
    return str(ini)


# ---------------------------------------------------------------------------
# compose_painted_3mf places the object on the plate
# ---------------------------------------------------------------------------


class TestPaintedBedPlacement:
    def test_negative_mesh_is_centred_on_default_plate(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        assert result["success"], result
        tx, ty, tz = _item_translation(result["output_path"])
        # bbox x -30..-20 (centre -25), y -31..-21 (centre -26) -> 128,128
        assert tx == pytest.approx(128.0 - (-25.0))
        assert ty == pytest.approx(128.0 - (-26.0))
        assert tz == pytest.approx(0.0)
        assert result["bed_translation"] == [
            pytest.approx(153.0), pytest.approx(154.0), 0.0,
        ]

    def test_on_bed_mesh_keeps_identity_transform(self, tmp_path):
        result = compose_painted_3mf(
            _POS_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        assert result["success"], result
        assert _item_translation(result["output_path"]) == (0.0, 0.0, 0.0)
        assert "bed_translation" not in result

    def test_below_plate_mesh_is_lifted(self, tmp_path):
        sunk = [
            tuple((v[0] + 100, v[1] + 100, v[2] - 4) for v in tri)
            for tri in _NEG_TETRA
        ]
        result = compose_painted_3mf(
            sunk, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        assert result["success"], result
        _tx, _ty, tz = _item_translation(result["output_path"])
        assert tz == pytest.approx(4.0)

    def test_explicit_plate_dims_set_the_centre(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf"),
            plate_width=200.0, plate_depth=180.0,
        )
        assert result["success"], result
        tx, ty, _tz = _item_translation(result["output_path"])
        assert tx == pytest.approx(100.0 - (-25.0))
        assert ty == pytest.approx(90.0 - (-26.0))

    def test_printer_id_resolves_build_volume(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf"),
            plate_width=999.0, plate_depth=999.0, printer_id="bambu_a1",
        )
        assert result["success"], result
        tx, ty, _tz = _item_translation(result["output_path"])
        assert tx == pytest.approx(128.0 - (-25.0))
        assert ty == pytest.approx(128.0 - (-26.0))

    def test_unknown_printer_id_falls_back_without_failing(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf"),
            printer_id="not_a_printer_model_zzz",
        )
        assert result["success"], result
        tx, _ty, _tz = _item_translation(result["output_path"])
        assert tx == pytest.approx(153.0)  # default 256 plate centre

    def test_paint_channels_survive_the_placement(self, tmp_path):
        """The transform must not disturb the dual-channel paint encoding."""
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        with zipfile.ZipFile(result["output_path"]) as zf:
            xml = zf.read("3D/3dmodel.model").decode()
            names = zf.namelist()
        assert xml.count("slic3rpe:mmu_segmentation") == 3
        assert xml.count("paint_color") == 3
        assert "Metadata/Slic3r_PE_model.config" in names


# ---------------------------------------------------------------------------
# compute_3mf_geometry_bbox applies the build transform
# ---------------------------------------------------------------------------


class TestGeometryBbox:
    def test_transform_is_applied(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        bbox = compute_3mf_geometry_bbox(result["output_path"])
        assert bbox is not None
        # Centred by the baked transform: on the plate, around (128, 128).
        assert bbox["x_min"] == pytest.approx(123.0)
        assert bbox["x_max"] == pytest.approx(133.0)
        assert bbox["y_min"] == pytest.approx(123.0)
        assert bbox["y_max"] == pytest.approx(133.0)

    def test_identity_transform_keeps_raw_coords(self, tmp_path):
        path = _write_3mf(tmp_path / "raw.3mf", _off_bed_model_xml())
        bbox = compute_3mf_geometry_bbox(path)
        assert bbox is not None
        assert bbox["x_min"] == pytest.approx(-30.0)
        assert bbox["y_max"] == pytest.approx(-21.0)

    def test_unparseable_archive_returns_none(self, tmp_path):
        junk = tmp_path / "junk.3mf"
        junk.write_bytes(b"not a zip")
        assert compute_3mf_geometry_bbox(str(junk)) is None


# ---------------------------------------------------------------------------
# slice_file: the pre-slice bed gate and the stderr surfacing
# ---------------------------------------------------------------------------


class TestBedBoundsFromProfile:
    def test_corner_origin_bed(self, tmp_path):
        assert _bed_xy_bounds_from_profile(_bambu_bed_ini(tmp_path)) == (
            0.0, 0.0, 256.0, 256.0,
        )

    def test_no_bed_shape_returns_none(self, tmp_path):
        ini = tmp_path / "nobed.ini"
        ini.write_text("layer_height = 0.2\n")
        assert _bed_xy_bounds_from_profile(str(ini)) is None

    def test_missing_profile_returns_none(self):
        assert _bed_xy_bounds_from_profile(None) is None
        assert _bed_xy_bounds_from_profile("/nonexistent.ini") is None


class TestPreSliceBedGate:
    def test_off_bed_3mf_is_refused_with_bbox_and_bed(self, tmp_path):
        threemf = _write_3mf(tmp_path / "off.3mf", _off_bed_model_xml())
        ini = _bambu_bed_ini(tmp_path)
        with patch(
            "subprocess.run",
            side_effect=AssertionError("slicer must not launch"),
        ), pytest.raises(SlicerError) as exc:
            slice_file(threemf, profile=ini, output_dir=str(tmp_path))
        msg = str(exc.value)
        assert "X[-30.0..-20.0]" in msg
        assert "Y[-31.0..-21.0]" in msg
        assert "X[0..256]" in msg

    def test_oversize_3mf_gets_rescale_advice_not_translate(self, tmp_path):
        """A footprint larger than the bed can't be fixed by moving it —
        the error must say rescale/split, not 'centre it'."""
        big = _off_bed_model_xml().replace('x="-30"', 'x="-300"').replace(
            'x="-20"', 'x="300"'
        )
        threemf = _write_3mf(tmp_path / "big.3mf", big)
        with patch(
            "subprocess.run",
            side_effect=AssertionError("slicer must not launch"),
        ), pytest.raises(SlicerError) as exc:
            slice_file(
                threemf, profile=_bambu_bed_ini(tmp_path),
                output_dir=str(tmp_path),
            )
        msg = str(exc.value)
        assert "rescale" in msg
        assert "translate the model" not in msg

    def test_on_bed_3mf_passes_the_gate(self, tmp_path):
        result = compose_painted_3mf(
            _NEG_TETRA, _COLORS, output_path=str(tmp_path / "p.3mf")
        )
        out_file = tmp_path / "p.gcode"

        mock_run = MagicMock(returncode=0, stdout="Done", stderr="")

        def fake_run(*args, **kwargs):
            out_file.write_text("G1 X1 Y1\n")
            return mock_run

        from kiln.slicer import SlicerInfo

        with patch(
            "kiln.slicer.find_slicer",
            return_value=SlicerInfo(path="/fake/prusa-slicer", name="prusa-slicer"),
        ), patch("subprocess.run", side_effect=fake_run):
            sliced = slice_file(
                result["output_path"],
                profile=_bambu_bed_ini(tmp_path),
                output_dir=str(tmp_path),
                output_name="p.gcode",
            )
        assert sliced.success


class TestStderrSurfaced:
    def test_exit_zero_no_output_reports_stderr(self, tmp_path):
        stl = tmp_path / "m.stl"
        stl.write_bytes(b"solid m\nendsolid m\n")
        mock_run = MagicMock(
            returncode=0,
            stdout="",
            stderr="All objects are outside of the print volume.\n",
        )
        from kiln.slicer import SlicerInfo

        with patch(
            "kiln.slicer.find_slicer",
            return_value=SlicerInfo(path="/fake/prusa-slicer", name="prusa-slicer"),
        ), patch("subprocess.run", return_value=mock_run), pytest.raises(SlicerError) as exc:
            slice_file(str(stl), output_dir=str(tmp_path))
        assert "outside of the print volume" in str(exc.value)


# ---------------------------------------------------------------------------
# compose_multicolor_3mf: the sibling door places the GROUP on the plate
# ---------------------------------------------------------------------------


def _write_cube_stl(path: Path, size: float, off: tuple[float, float, float]) -> str:
    import struct

    v = [
        (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
        (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
    ]
    v = [(a + off[0], b + off[1], c + off[2]) for a, b, c in v]
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


def _item_translations(threemf_path: str) -> list[tuple[float, float, float]]:
    with zipfile.ZipFile(threemf_path) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    out = []
    for m in re.finditer(r'<item objectid="\d+"\s+transform="([^"]+)"', xml):
        values = [float(v) for v in m.group(1).split()]
        assert len(values) == 12
        out.append((values[9], values[10], values[11]))
    assert out, "no build items found"
    return out


class TestMulticolorGroupPlacement:
    def test_off_plate_group_gets_one_common_shift(self, tmp_path):
        """Two parts off the plate move together: relative layout intact."""
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        a = _write_cube_stl(tmp_path / "a.stl", 20.0, (-10.0, -10.0, 0.0))
        b = _write_cube_stl(tmp_path / "b.stl", 10.0, (-45.0, -5.0, 0.0))
        result = compose_multicolor_3mf(
            [
                ColorPart(a, extruder=1, name="body"),
                ColorPart(b, extruder=2, name="accent"),
            ],
            output_path=str(tmp_path / "mc.3mf"),
        )
        assert result["success"], result
        # union bbox x -45..10 (centre -17.5), y -10..10 (centre 0)
        expect = (128.0 - (-17.5), 128.0 - 0.0, 0.0)
        translations = _item_translations(result["output_path"])
        assert len(translations) == 2
        for t in translations:
            assert t == pytest.approx(expect)
        assert result["bed_translation"] == [
            pytest.approx(145.5), pytest.approx(128.0), 0.0,
        ]

    def test_on_plate_group_is_untouched(self, tmp_path):
        """Arranged parts (auto_arrange_parts output) keep their placement."""
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        a = _write_cube_stl(tmp_path / "a.stl", 20.0, (0.0, 0.0, 0.0))
        result = compose_multicolor_3mf(
            [ColorPart(a, extruder=1, name="body", x=30.0, y=40.0)],
            output_path=str(tmp_path / "mc.3mf"),
        )
        assert result["success"], result
        assert _item_translations(result["output_path"]) == [(30.0, 40.0, 0.0)]
        assert "bed_translation" not in result

    def test_gate_accepts_the_placed_group(self, tmp_path):
        """The written file passes slice_file's pre-slice bed gate."""
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        a = _write_cube_stl(tmp_path / "a.stl", 20.0, (-10.0, -10.0, 0.0))
        result = compose_multicolor_3mf(
            [ColorPart(a, extruder=1, name="body")],
            output_path=str(tmp_path / "mc.3mf"),
        )
        bbox = compute_3mf_geometry_bbox(result["output_path"])
        assert bbox is not None
        assert bbox["x_min"] == pytest.approx(118.0)
        assert bbox["x_max"] == pytest.approx(138.0)


# ---------------------------------------------------------------------------
# The regression, end to end against a real PrusaSlicer
# ---------------------------------------------------------------------------


def _real_prusaslicer() -> str | None:
    for name in ("prusa-slicer", "PrusaSlicer", "prusaslicer"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
def test_painted_negative_mesh_slices_end_to_end(tmp_path):
    """The original repro: paint a mesh that is not centred on the bed,
    export, slice with PrusaSlicer, and REQUIRE gcode.  Before the fix
    this died as exit 0 / no output / "output file was not created"."""
    # A 20mm cube centred on its own origin — every x/y in [-10, 10].
    s = 10.0
    v = [
        (-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0),
        (-s, -s, 2 * s), (s, -s, 2 * s), (s, s, 2 * s), (-s, s, 2 * s),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    tris = [tuple(v[i] for i in f) for f in faces]
    colors: list[str | None] = ["#ff0000"] * 6 + ["#0000ff"] * 6

    from kiln.slicer_profiles import resolve_slicer_profile

    result = compose_painted_3mf(
        tris, colors, output_path=str(tmp_path / "painted.3mf"),
        printer_id="bambu_a1",
    )
    assert result["success"], result
    assert "bed_translation" in result

    sliced = slice_file(
        result["output_path"],
        profile=resolve_slicer_profile("bambu_a1"),
        slicer_path=_real_prusaslicer(),
        output_dir=str(tmp_path / "out"),
        timeout=300,
    )
    assert sliced.success
    assert os.path.getsize(sliced.output_path) > 0


@pytest.mark.skipif(_real_prusaslicer() is None, reason="needs a real PrusaSlicer")
def test_multicolor_negative_group_slices_end_to_end(tmp_path):
    """The sibling door: a zone-style multi-object 3MF whose parts sit at
    their raw (negative) STL coordinates must also reach gcode."""
    from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf
    from kiln.slicer_profiles import resolve_slicer_profile

    a = _write_cube_stl(tmp_path / "a.stl", 20.0, (-10.0, -10.0, 0.0))
    b = _write_cube_stl(tmp_path / "b.stl", 10.0, (-45.0, -5.0, 0.0))
    result = compose_multicolor_3mf(
        [
            ColorPart(a, extruder=1, name="body", color="#AAAAAA"),
            ColorPart(b, extruder=2, name="accent", color="#111111"),
        ],
        output_path=str(tmp_path / "mc.3mf"),
    )
    assert result["success"], result
    assert "bed_translation" in result

    sliced = slice_file(
        result["output_path"],
        profile=resolve_slicer_profile("bambu_a1"),
        slicer_path=_real_prusaslicer(),
        output_dir=str(tmp_path / "out"),
        timeout=300,
    )
    assert sliced.success
    assert os.path.getsize(sliced.output_path) > 0
