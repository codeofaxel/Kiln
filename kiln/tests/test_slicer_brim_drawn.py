"""The brim Kiln decides is the brim the file has, in every slicer it drives.

The defect this pins: Kiln writes the brim it decided as ``brim_width``,
PrusaSlicer's spelling, and PrusaSlicer draws exactly that.  Kiln's
Orca-dialect presets never stated ``brim_type``, so OrcaSlicer and
BambuStudio stayed on their own default, ``auto_brim``, which picks its own
brim whatever the width.  The person was told one brim and the printer was
sent another.

A second half surfaced once the brim was real: these two slicers cut a brim
off at the bed's edge instead of printing it past it, so a brim too wide for
the room beside a part reached the printer narrower than asked, on the very
side that holds the part down, while the slice said "kept the 5 mm brim".
PrusaSlicer prints it past the edge and Kiln refuses with the width that
fits; that refusal now comes from all three.  And on a multicolour plate the
prime tower, placed clear of the part, sat on the part's now-real brim.

The slicer cases read the sliced G-code's own feature labels and
coordinates, never its config lines, and are skipped where the slicer is not
installed; the preset cases at the end run everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln.printers import bed_fit
from kiln.slicer import slice_file
from kiln.slicer_geometry import parse_slicer_features
from kiln.slicer_orca import settings_to_orca_presets
from kiln.slicer_profiles import profile_with_overrides, resolve_slicer_profile

trimesh = pytest.importorskip("trimesh")

_ORCA = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
_BAMBU = "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
#: The Bambu Lab A1's bed, which the bundled ``bambu_a1`` profile states.
_A1 = "bambu_a1"
_A1_BED = (256.0, 256.0, 256.0)


def _installed(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


_SLICERS = [
    pytest.param(_ORCA, id="orcaslicer", marks=pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")),
    pytest.param(_BAMBU, id="bambustudio", marks=pytest.mark.skipif(not _installed(_BAMBU), reason="BambuStudio not installed")),
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from kiln import monitor_twin

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    twin = tmp_path / "twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", twin)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", twin / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", twin / "active.json")
    # The A1's bed, stated where every bed read starts, so the edge cases do
    # not lean on whichever catalogue record this process holds.
    monkeypatch.setitem(bed_fit._BUILD_VOLUME_OVERRIDES, _A1, _A1_BED)


def _frame(tmp_path: Path) -> str:
    """A 30 mm square frame with a 12 mm hole, 6 mm tall: an outside edge and an inside one."""
    frame = trimesh.creation.box(extents=(30, 30, 6)).difference(trimesh.creation.box(extents=(12, 12, 8)))
    frame.apply_translation((0, 0, 3))
    path = tmp_path / "frame.stl"
    frame.export(str(path))
    return str(path)


def _pillar(tmp_path: Path) -> str:
    """4 x 4 x 60 mm: the shape an automatic brim adds itself to."""
    pillar = trimesh.creation.box(extents=(4, 4, 60))
    pillar.apply_translation((0, 0, 30))
    path = tmp_path / "pillar.stl"
    pillar.export(str(path))
    return str(path)


def _slice(path: str, tmp_path: Path, slicer: str, overrides: dict[str, str] | None) -> str:
    profile = resolve_slicer_profile(_A1)
    if overrides:
        profile = profile_with_overrides(profile, overrides)
    out = tmp_path / f"out_{len(list(tmp_path.glob('out_*')))}"
    return slice_file(path, output_dir=str(out), profile=profile, slicer_path=slicer, timeout=600).output_path


def _brim(gcode: str) -> tuple[list[float], list[float], tuple[float, float, float, float]]:
    """The brim's toolpath points and the part's footprint, from the file."""
    parsed = parse_slicer_features(gcode)
    assert parsed.labelled and parsed.model_footprint is not None
    bucket = parsed.buckets.get("brim")
    segs = bucket.segments if bucket is not None else []
    return segs[0::6] + segs[3::6], segs[1::6] + segs[4::6], parsed.model_footprint


def _brim_outside(gcode: str) -> list[float]:
    """How far the brim reaches past the part on each side (left, front, right, back)."""
    xs, ys, (x0, y0, x1, y1) = _brim(gcode)
    assert xs, "the file draws no brim"
    return [x0 - min(xs), y0 - min(ys), max(xs) - x1, max(ys) - y1]


def _brim_in_the_hole(gcode: str) -> int:
    xs, ys, (x0, y0, x1, y1) = _brim(gcode)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return sum(1 for x, y in zip(xs, ys, strict=True) if abs(x - cx) < 6 and abs(y - cy) < 6)


# ---------------------------------------------------------------------------
# The brim in the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slicer", _SLICERS)
class TestTheBrimInTheFile:
    def test_a_brim_asked_for_is_drawn_round_the_part_at_its_width(self, tmp_path, slicer):
        gcode = _slice(_frame(tmp_path), tmp_path, slicer, {"brim_width": "5"})
        for side in _brim_outside(gcode):
            assert side == pytest.approx(5.0, abs=0.5), _brim_outside(gcode)
        assert _brim_in_the_hole(gcode) == 0, "a brim round the outside, not in the hole"

    @pytest.mark.parametrize("overrides", [{"brim_width": "0"}, None], ids=["brim_width_0", "no_brim_stated"])
    def test_no_brim_asked_for_is_no_brim_drawn(self, tmp_path, slicer, overrides):
        xs, _ys, _fp = _brim(_slice(_pillar(tmp_path), tmp_path, slicer, overrides))
        assert xs == [], "the slicer added a brim nobody asked for"


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
class TestTheBrimTypeAsked:
    def test_a_brim_only_in_the_holes_stays_in_the_holes(self, tmp_path):
        gcode = _slice(_frame(tmp_path), tmp_path, _ORCA, {"brim_width": "5", "brim_type": "inner_only"})
        xs, ys, (x0, y0, x1, y1) = _brim(gcode)
        assert _brim_in_the_hole(gcode) > 0
        assert min(xs) > x0 and min(ys) > y0 and max(xs) < x1 and max(ys) < y1, "nothing round the outside"

    def test_a_type_prusaslicer_does_not_know_is_drawn_as_prusaslicer_draws_it(self, tmp_path):
        """PrusaSlicer reads ``auto_brim`` as its default, the brim all round
        (measured: 2.9.4 wrote ``; brim_type = outer_only``), so the same
        profile prints the same brim in either slicer."""
        gcode = _slice(_frame(tmp_path), tmp_path, _ORCA, {"brim_width": "5", "brim_type": "auto_brim"})
        for side in _brim_outside(gcode):
            assert side == pytest.approx(5.0, abs=0.5)


# ---------------------------------------------------------------------------
# A brim beside a prime tower
# ---------------------------------------------------------------------------


def _two_colours(tmp_path: Path) -> str:
    """Two 20 mm cubes side by side mid-plate, one per filament: a
    multicolour plate, which prints with a prime tower beside it."""
    from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

    parts = []
    for name, x, colour, extruder in (("red", 118.0, "#FF0000", 1), ("blue", 142.0, "#0000FF", 2)):
        cube = trimesh.creation.box(extents=(20, 20, 10))
        cube.apply_translation((x, 128, 5))
        stl = tmp_path / f"{name}.stl"
        cube.export(str(stl))
        parts.append(ColorPart(str(stl), extruder=extruder, color=colour, name=name))
    composed = compose_multicolor_3mf(parts, output_path=str(tmp_path / "two.3mf"), printer_id=_A1)
    assert composed["success"], composed
    return composed["output_path"]


@pytest.mark.skipif(not _installed(_ORCA), reason="OrcaSlicer not installed")
def test_a_multicolour_plate_draws_its_brim_clear_of_the_prime_tower(tmp_path):
    """OrcaSlicer only: BambuStudio 02.08.02.61 refuses this plate with or
    without a brim ("found slicing or export error", exit 156)."""
    gcode = _slice(_two_colours(tmp_path), tmp_path, _ORCA, {"brim_width": "8"})
    xs, ys, (x0, y0, x1, y1) = _brim(gcode)
    assert xs, "the file draws no brim"
    brim = (min(xs), min(ys), max(xs), max(ys))
    assert min(x0 - brim[0], y0 - brim[1], brim[2] - x1, brim[3] - y1) == pytest.approx(8.0, abs=0.6)
    tower = parse_slicer_features(gcode).buckets["prime_tower"].segments
    tx, ty = tower[0::3], tower[1::3]
    apart = min(tx) > brim[2] or max(tx) < brim[0] or min(ty) > brim[3] or max(ty) < brim[1]
    assert apart, f"the brim {brim} runs into the tower {(min(tx), min(ty), max(tx), max(ty))}"


# ---------------------------------------------------------------------------
# A brim at the bed's edge
# ---------------------------------------------------------------------------


def _near_the_edge(tmp_path: Path) -> str:
    """A 20 mm cube 3 mm in from the bed's front-left edges, as a 3MF: a
    5 mm brim has no room there, a 3 mm one does."""
    from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

    cube = trimesh.creation.box(extents=(20, 20, 20))
    cube.apply_translation((13, 13, 10))
    stl = tmp_path / "cube.stl"
    cube.export(str(stl))
    composed = compose_multicolor_3mf(
        [ColorPart(str(stl), extruder=1, name="cube")], output_path=str(tmp_path / "near.3mf"),
    )
    assert composed["success"], composed
    return composed["output_path"]


def _reslice(path: str, tmp_path: Path, slicer: str, brim_width: str) -> dict:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict = {}

    class _FakeMcp:
        def tool(self, name=None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    with patch("kiln.server._check_auth", return_value=None):
        return tools["reslice_with_overrides"](
            input_path=path, printer_id=_A1, overrides={"brim_width": brim_width},
            output_dir=str(tmp_path / f"out_{brim_width}"), slicer_path=slicer, auto_center=False,
        )


@pytest.mark.parametrize("slicer", _SLICERS)
class TestABrimAtTheBedEdge:
    def test_a_brim_with_no_room_is_refused_with_the_width_that_fits(self, tmp_path, slicer):
        near = _near_the_edge(tmp_path)
        refused = _reslice(near, tmp_path, slicer, "5")
        assert refused.get("success") is False, refused
        assert refused["error"]["code"] == "BRIM_PAST_BED", refused
        message = refused["error"]["message"]
        assert "5 mm brim" in message and "brim_width=3" in message, message
        assert "toward the middle of the bed" in message

    def test_the_width_it_names_prints_in_full_on_the_bed(self, tmp_path, slicer):
        kept = _reslice(_near_the_edge(tmp_path), tmp_path, slicer, "3")
        assert kept.get("success"), kept
        gcode = kept["output_path"]
        if gcode.endswith(".3mf"):  # wrapped for the A1; the plate's G-code is what prints
            import zipfile

            with zipfile.ZipFile(gcode) as zf:
                name = next(n for n in zf.namelist() if n.endswith(".gcode"))
                gcode = str(tmp_path / "plate.gcode")
                Path(gcode).write_bytes(zf.read(name))
        sides = _brim_outside(gcode)
        # Whole loops: OrcaSlicer fits 2.5 mm of them into 3 (measured), the
        # same on every side -- nothing cut off on the side at the edge.
        assert max(sides) - min(sides) < 0.1, sides
        assert min(sides) == pytest.approx(3.0, abs=0.6), sides
        assert bed_fit.check_bed_fit(bed_fit.compute_gcode_bbox(gcode), _A1_BED, source="gcode")["ok"]
        assert "kept the 3 mm brim" in kept["message"], kept["message"]


# ---------------------------------------------------------------------------
# The preset, where no slicer is installed
# ---------------------------------------------------------------------------


class TestThePreset:
    """The process preset states the type on every slice; the slicer tests
    above are what prove what each type draws."""

    @pytest.mark.parametrize(
        ("settings", "stated"),
        [
            ({"brim_width": "5"}, "outer_only"),
            ({"brim_width": "0"}, "no_brim"),
            ({}, "no_brim"),
            ({"brim_width": "5", "brim_type": "no_brim"}, "no_brim"),
            ({"brim_width": "5", "brim_type": "inner_only"}, "inner_only"),
            ({"brim_width": "5", "brim_type": "outer_and_inner"}, "outer_and_inner"),
            ({"brim_width": "5", "brim_type": "auto_brim"}, "outer_only"),
            ({"brim_width": "0", "brim_type": "outer_only"}, "no_brim"),
        ],
    )
    def test_the_type_is_always_stated(self, settings, stated):
        assert settings_to_orca_presets(settings).process["brim_type"] == stated
