"""A person's own speeds for height ranges, put into the slice.

Contract pinned here
--------------------
* A wish that cannot be printed as said is refused before anything is sliced.
* The reply is measured from the sliced file: each range says what was asked,
  what the file commands, and one outcome; a range the slicer did not apply
  refuses the slice and leaves no file.
* With a real slicer: the feeds inside each range are the ones asked, the
  band outside the ranges is the same as a slice without the wish, and no
  move asks for more plastic than the profile's flow limit.
* The wish reaches a version's record from Kiln's own record of the slice,
  never from a caller.
* Every slice door hands the wish to the one slicing function.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln import speed_by_height as sbh
from kiln.slicer import SlicerError, SliceResult, owner_asks, slice_file


@pytest.fixture(autouse=True)
def _own_slice_folder(tmp_path):
    """Each test slices into its own folder: a slice is written to a path
    fixed by the part's name, so tests running side by side would otherwise
    read each other's file."""
    from kiln.output_paths import REQUEST_OUTPUT_ROOT

    (tmp_path / "slices").mkdir(exist_ok=True)
    token = REQUEST_OUTPUT_ROOT.set(str(tmp_path / "slices"))
    yield
    REQUEST_OUTPUT_ROOT.reset(token)


# ---------------------------------------------------------------------------
# Parts and files
# ---------------------------------------------------------------------------


def _box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> list[tuple]:
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def _write_stl(path: Path, triangles: list[tuple]) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for corner in tri:
                fh.write(struct.pack("<3f", *corner))
            fh.write(b"\0\0")
    return str(path)


def _stepped_part(path: Path) -> str:
    """A 30 mm square base 3 mm tall with a 12 mm square post on it, to 8 mm."""
    return _write_stl(path, _box(0, 0, 0, 30, 30, 3) + _box(9, 9, 2.9, 21, 21, 8))


_FILAMENT_AREA = math.pi * (1.75 / 2.0) ** 2


def _synthetic(
    path: Path,
    speeds: dict[float, Any],
    *,
    side_mm: float = 40.0,
    layer_mm: float = 0.2,
    flow_mm3_s: dict[float, float] | None = None,
    settings: dict[str, str] | None = None,
    seconds: int = 600,
    lifted_mm: float = 0.0,
    support_mm_s: float = 0.0,
) -> str:
    """A G-code of one square wall a layer: ``speeds`` is layer top (mm) ->
    mm/s (or one speed for each side of the square), with relative extrusion
    and the layer lines a slicer writes.  ``lifted_mm`` prints each layer's
    last side that much above the layer, as a move does while the nozzle
    is still rising."""
    lines = ["M83", "G1 Z5 F6000"]
    for z, stated_speed in sorted(speeds.items()):
        flow = (flow_mm3_s or {}).get(z, 2.0)
        sides = stated_speed if isinstance(stated_speed, tuple) else (stated_speed,) * 4
        lines += [";LAYER_CHANGE", f";Z:{z:g}", f";HEIGHT:{layer_mm:g}", f"G1 Z{z:g} F6000", ";TYPE:Perimeter",
                  "G1 X0 Y0 F9000"]
        for side, (x, y) in enumerate(((side_mm, 0), (side_mm, side_mm), (0, side_mm), (0, 0))):
            speed = sides[side]
            if lifted_mm and side == 3:
                lines.append(f"G1 Z{z + lifted_mm:g} F6000")
            lines.append(f"G1 X{x:g} Y{y:g} E{flow / speed / _FILAMENT_AREA * side_mm:.5f} F{speed * 60:g}")
        if support_mm_s:
            lines += [";TYPE:Support material", f"G1 X{side_mm:g} Y{side_mm:g} E1 F{support_mm_s * 60:g}"]
    stated = {"filament_diameter": "1.75", "layer_height": f"{layer_mm:g}", "perimeter_speed": "60",
              "infill_speed": "100", "max_volumetric_speed": "11", **(settings or {})}
    hours, rest = divmod(seconds, 3600)
    lines.append(f"; estimated printing time (normal mode) = {hours}h {rest // 60}m {rest % 60}s")
    lines += [f"; {key} = {value}" for key, value in stated.items()]
    lines.append("; filament used [mm] = 100")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _layers(first: float, last: float, step: float = 0.2) -> list[float]:
    count = int(round((last - first) / step)) + 1
    return [round(first + i * step, 3) for i in range(count)]


class _FakeSlicer:
    """Stands in for the slicer under :func:`kiln.speed_by_height.sliced`:
    writes one file for the slice without the wish and another for the slice
    with it, and keeps what it was handed."""

    def __init__(self, tmp_path: Path, plain: dict[float, float], made: dict[float, float], **synthetic: Any) -> None:
        self.tmp_path, self.plain, self.made, self.synthetic = tmp_path, plain, made, synthetic
        self.part = _stepped_part(tmp_path / "part.stl")
        self.calls: list[dict[str, Any]] = []
        self.handed: list[str] = []

    def __call__(self, out_path: str, *, prepare: Any, first_layer_speed: float | None = None, record: bool) -> SliceResult:
        self.calls.append({"record": record, "first_layer_speed": first_layer_speed})
        handed_dir = self.tmp_path / f"handed_{len(self.calls)}"
        handed_dir.mkdir()
        self.handed.append(prepare(self.part, str(handed_dir), (100.0, 100.0)))
        made_kwargs = dict(self.synthetic)
        if record:
            made_kwargs["flow_mm3_s"] = made_kwargs.pop("made_flow_mm3_s", made_kwargs.get("flow_mm3_s"))
            made_kwargs["seconds"] = 720
        else:
            made_kwargs.pop("made_flow_mm3_s", None)
        _synthetic(Path(out_path), self.made if record else self.plain, **made_kwargs)
        return SliceResult(success=True, output_path=out_path, slicer="fake", message="Sliced part.stl -> part.gcode")


def _slice_with(fake: _FakeSlicer, wish: Any, *, family: str = sbh.PRUSA) -> SliceResult:
    out = fake.tmp_path / "out" / "part.gcode"
    out.parent.mkdir(exist_ok=True)
    return sbh.sliced(sbh.checked(wish), family=family, out_file=str(out), run=fake)


# ---------------------------------------------------------------------------
# The wish itself
# ---------------------------------------------------------------------------


class TestAWishIsCheckedBeforeAnythingIsSliced:
    @pytest.mark.parametrize(
        ("wish", "says"),
        [
            ([{"from_mm": 5, "to_mm": 5, "percent": 50}], "has no height"),
            ([{"from_mm": 6, "to_mm": 2, "percent": 50}], "has no height"),
            ([{"from_mm": 0, "to_mm": 6, "percent": 50}, {"from_mm": 5, "to_mm": 9, "speed_mm_s": 20}], "overlap between 5 mm and 6 mm"),
            ([{"from_mm": 0, "percent": 50}, {"from_mm": 3, "percent": 60}], "overlap"),
            ([{"from_mm": -1, "percent": 50}], "below the plate"),
            ([{"from_mm": 0, "to_mm": 4}], "needs one speed"),
            ([{"from_mm": 0, "to_mm": 4, "percent": 50, "speed_mm_s": 20}], "needs one speed"),
            ([{"from_mm": 0, "speed_mm_s": 5000}], "from 1 to 1000 mm/s"),
            ([{"from_mm": 0, "percent": 1}], "percent is from 5 to 500"),
            ([{"from": 0, "percent": 50}], "is not read"),
            ("not json", "could not be read"),
            (7, "takes a list of height ranges"),
        ],
    )
    def test_a_wish_that_cannot_be_printed_as_said_is_refused_in_plain_words(self, wish, says):
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            sbh.checked(wish)
        assert says in str(refused.value)
        assert isinstance(refused.value, SlicerError)

    def test_no_wish_is_no_ranges(self):
        assert sbh.checked(None) == [] and sbh.checked([]) == [] and sbh.checked("") == []

    def test_ranges_are_read_lowest_first_and_an_open_top_is_kept(self):
        ranges = sbh.checked('[{"from_mm": 6, "speed_mm_s": 120}, {"from_mm": 0, "to_mm": 6, "percent": 50}]')
        assert [(r.from_mm, r.to_mm, r.percent, r.speed_mm_s) for r in ranges] == [
            (0.0, 6.0, 50.0, None), (6.0, None, None, 120.0),
        ]

    def test_slice_file_refuses_a_bad_wish_without_looking_for_a_slicer(self, tmp_path):
        part = _stepped_part(tmp_path / "part.stl")
        with patch("kiln.slicer.find_slicer", side_effect=AssertionError("looked for a slicer")), \
                pytest.raises(sbh.SpeedByHeightError, match="has no height"):
            slice_file(part, speed_by_height=[{"from_mm": 3, "to_mm": 3, "percent": 50}])


# ---------------------------------------------------------------------------
# The read-back, on files of known speeds
# ---------------------------------------------------------------------------


class TestTheReplyIsMeasuredFromTheSlicedFile:
    def test_a_slow_range_the_file_obeys_is_applied_with_its_numbers(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        result = _slice_with(_FakeSlicer(tmp_path, plain, made), [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        block = result.speed_by_height
        (band,) = block["ranges"]
        assert band["outcome"] == sbh.APPLIED
        assert band["asked"] == {"speed_mm_s": 20.0}
        assert band["measured"]["fastest_mm_s"] == 20.0 and band["measured"]["slowest_mm_s"] == 20.0
        assert band["measured"]["layers"] == 20
        assert (band["measured"]["first_layer_z_mm"], band["measured"]["last_layer_z_mm"]) == (2.2, 6.0)
        assert band["without"]["fastest_mm_s"] == 60.0
        assert block["outside_ranges"] == "unchanged"
        assert (block["estimate_seconds"], block["estimate_without_seconds"]) == (720, 600)
        assert "applied" in result.message and "20.0 mm/s" in result.message
        assert os.path.isfile(result.output_path)

    def test_the_slice_without_the_wish_is_made_first_and_leaves_no_record(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made)
        _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        assert [call["record"] for call in fake.calls] == [False, True]

    def test_a_range_the_slicer_ignored_refuses_the_slice_and_keeps_no_file(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        fake = _FakeSlicer(tmp_path, plain, dict(plain))
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED"
        assert "no file was kept" in str(refused.value) and "still runs faster than asked" in str(refused.value)
        assert refused.value.ranges[0]["outcome"] == sbh.NOT_APPLIED
        assert not (tmp_path / "out" / "part.gcode").exists()

    def test_a_faster_range_the_slicer_ignored_is_refused_too(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        fake = _FakeSlicer(tmp_path, plain, dict(plain), settings={"cooling": "0"})
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 200}])
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED"
        assert "exactly as it does without the wish" in str(refused.value)

    def test_a_section_the_slicer_already_runs_slower_says_nothing_changed(self, tmp_path):
        # A 4 mm square at 10 mm/s: 1.6 s a layer, under the 5 s the file says it cools below.
        plain = dict.fromkeys(_layers(0.2, 10.0), 10.0)
        fake = _FakeSlicer(tmp_path, plain, dict(plain), side_mm=4.0,
                           settings={"cooling": "1", "slowdown_below_layer_time": "5"})
        result = _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.ALREADY_SLOWER
        assert "already this slow or slower here" in band["said"] and "nothing changed" in band["said"]
        assert band["measured"]["fastest_mm_s"] == 10.0

    def test_a_fast_range_held_at_the_flow_limit_is_capped_and_says_what_it_reached(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (150.0 if z > 6.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made, made_flow_mm3_s={z: 11.0 for z in plain if z > 6.0})
        result = _slice_with(fake, [{"from_mm": 6, "speed_mm_s": 300}])
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.CAPPED
        assert band["measured"]["fastest_mm_s"] == 150.0
        assert band["measured"]["peak_flow_mm3_s"] == pytest.approx(11.0, abs=0.05)
        assert "what the nozzle can melt (11 mm³/s)" in band["said"] and "150.0 mm/s" in band["said"]

    def test_a_fast_range_held_below_the_flow_limit_is_limited_not_capped(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (90.0 if z > 6.0 else 60.0) for z in plain}
        result = _slice_with(_FakeSlicer(tmp_path, plain, made, settings={"cooling": "0"}),
                             [{"from_mm": 6, "speed_mm_s": 300}])
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.LIMITED and "90.0 mm/s" in band["said"]

    def test_a_range_above_the_part_is_refused_with_the_parts_own_height(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        fake = _FakeSlicer(tmp_path, plain, dict(plain))
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _slice_with(fake, [{"from_mm": 40, "to_mm": 50, "speed_mm_s": 20}])
        assert refused.value.code == "SPEED_BY_HEIGHT_OUTSIDE_PART"
        assert "holds no layer of this part" in str(refused.value) and "0.2 mm to 10 mm" in str(refused.value)
        assert [call["record"] for call in fake.calls] == [False]

    def test_speeds_changed_outside_the_ranges_are_said(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 55.0) for z in plain}
        result = _slice_with(_FakeSlicer(tmp_path, plain, made), [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        assert result.speed_by_height["outside_ranges"] == "changed"
        assert "Outside the ranges" in result.message

    def test_supports_that_keep_their_own_faster_speed_are_said(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        result = _slice_with(_FakeSlicer(tmp_path, plain, made, support_mm_s=80.0),
                             [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}])
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.APPLIED and band["measured"]["fastest_mm_s"] == 20.0
        assert band["supports"] == {"kept_own_speed": True, "fastest_mm_s": 80.0}
        assert "Supports in this range keep the profile's own support speed (up to 80.0 mm/s)" in result.message

    def test_a_move_belongs_to_the_layer_the_slicers_own_layer_line_names(self, tmp_path):
        heights = _layers(0.2, 4.0)
        lifted = sbh.read_gcode(_synthetic(tmp_path / "lifted.gcode", dict.fromkeys(heights, 60.0), lifted_mm=0.3))
        assert [layer.z for layer in lifted.layers] == heights
        assert all(len(layer.moves) == 4 for layer in lifted.layers)
        assert all(layer.height == pytest.approx(0.2) for layer in lifted.layers)

    def test_percent_is_taken_of_the_speeds_the_slice_itself_states(self, tmp_path):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (30.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made, settings={"external_perimeter_speed": "50%"})
        result = _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "percent": 50}])
        assert result.speed_by_height["ranges"][0]["outcome"] == sbh.APPLIED
        with zipfile.ZipFile(fake.handed[1]) as archive:
            ranges = archive.read("Metadata/Prusa_Slicer_layer_config_ranges.xml").decode()
        assert '<option opt_key="perimeter_speed">30</option>' in ranges
        assert '<option opt_key="infill_speed">50</option>' in ranges
        # A speed the slicer takes as a share of another follows it by itself.
        assert "external_perimeter_speed" not in ranges


class TestTheFirstLayer:
    def test_a_slow_range_from_the_plate_slows_the_first_layer_and_never_speeds_it(self, tmp_path):
        plain = {z: (20.0 if z == 0.2 else 60.0) for z in _layers(0.2, 10.0)}
        made = {z: (12.0 if z <= 2.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made)
        result = _slice_with(fake, [{"from_mm": 0, "to_mm": 2, "speed_mm_s": 12}])
        assert fake.calls[1]["first_layer_speed"] == 12.0
        assert result.speed_by_height["ranges"][0]["outcome"] == sbh.APPLIED

        (tmp_path / "b").mkdir()
        calm = _FakeSlicer(tmp_path / "b", plain, {z: (40.0 if z <= 2.0 and z != 0.2 else plain[z]) for z in plain})
        result = _slice_with(calm, [{"from_mm": 0, "to_mm": 2, "speed_mm_s": 40}])
        assert calm.calls[1]["first_layer_speed"] is None
        band = result.speed_by_height["ranges"][0]
        assert band["outcome"] == sbh.APPLIED
        assert band["first_layer"] == {"kept_own_speed": True, "fastest_mm_s": 20.0}
        assert "first layer keeps the profile's own first-layer speed" in band["said"]

    def test_a_first_layer_of_two_speeds_is_slowed_to_its_slower_one_at_most(self, tmp_path):
        # Walls at 20 and the rest at 60, asked for 40: the walls are never sped up to 40.
        plain = {z: ((20.0, 60.0, 20.0, 60.0) if z == 0.2 else 60.0) for z in _layers(0.2, 10.0)}
        made = {z: (20.0 if z == 0.2 else 40.0 if z <= 2.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made)
        result = _slice_with(fake, [{"from_mm": 0, "to_mm": 2, "speed_mm_s": 40}])
        assert fake.calls[1]["first_layer_speed"] == 20.0
        assert result.speed_by_height["ranges"][0]["outcome"] == sbh.APPLIED


class TestTheFileTheSlicerIsHanded:
    @pytest.mark.parametrize(
        ("family", "member", "key"),
        [
            (sbh.PRUSA, "Metadata/Prusa_Slicer_layer_config_ranges.xml", "external_perimeter_speed"),
            (sbh.BAMBU, "Metadata/layer_config_ranges.xml", "outer_wall_speed"),
        ],
    )
    def test_the_ranges_name_the_part_by_its_place_on_the_plate(self, tmp_path, family, member, key):
        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made)
        _slice_with(fake, [{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}], family=family)
        without, with_wish = fake.handed
        with zipfile.ZipFile(without) as archive:
            assert member not in archive.namelist()
        with zipfile.ZipFile(with_wish) as archive:
            ranges = archive.read(member).decode()
            model = archive.read("3D/3dmodel.model").decode()
        assert re.findall(r'<object id="(\d+)">', ranges) == ["1"]
        assert '<range min_z="2" max_z="6">' in ranges
        assert f'<option opt_key="{key}">20</option>' in ranges
        assert '<option opt_key="layer_height">0.2</option>' in ranges
        # The part stands on the plate, centred where the slicer was told.
        xs = [float(x) for x in re.findall(r'<vertex x="([-\d.e]+)"', model)]
        placed = re.search(r'transform="1 0 0 0 1 0 0 0 1 ([-\d.e]+) ([-\d.e]+) ([-\d.e]+)"', model)
        assert (min(xs) + max(xs)) / 2 + float(placed.group(1)) == pytest.approx(100.0)
        assert float(placed.group(3)) == pytest.approx(0.0)

    def test_a_3mf_that_has_height_ranges_of_its_own_is_refused(self, tmp_path):
        plate = sbh.plate_3mf(_stepped_part(tmp_path / "part.stl"), str(tmp_path), bed_centre=(100.0, 100.0))
        with zipfile.ZipFile(plate, "a") as archive:
            archive.writestr("Metadata/layer_config_ranges.xml", "<objects/>")
        (tmp_path / "out").mkdir()
        with pytest.raises(sbh.SpeedByHeightError, match="already carries height ranges of its own"):
            sbh.with_ranges(plate, str(tmp_path / "out"), [], sbh.BAMBU, top_mm=10.0)


# ---------------------------------------------------------------------------
# The wish on a record
# ---------------------------------------------------------------------------


class TestTheWishOnAVersionRecord:
    def _sliced_here(self, tmp_path: Path) -> tuple[str, dict]:
        from kiln import print_lineage

        part = _stepped_part(tmp_path / "part.stl")
        gcode = _synthetic(tmp_path / "part.gcode", dict.fromkeys(_layers(0.2, 4.0), 60.0))
        assert print_lineage.record_slice(part, gcode) is not None
        wish = {"ranges": [{"from_mm": 0.0, "to_mm": 2.0, "asked": {"percent": 50.0}, "outcome": "applied",
                            "fastest_mm_s": 30.0}], "estimate_seconds": 720, "estimate_without_seconds": 600}
        assert print_lineage.note_speed_by_height(gcode, wish) is True
        print_lineage.flush()
        return gcode, wish

    def test_a_version_keeps_the_wish_kilns_record_of_the_slice_states(self, tmp_path):
        gcode, wish = self._sliced_here(tmp_path)
        kept = sbh.keep_wish({"material": "PLA"}, gcode)
        assert kept == {"material": "PLA", sbh.RECORD_KEY: wish}
        assert sbh.wish_sentence(kept) == (
            "Sliced with its own speeds by height: 0 mm to 2 mm at 50% of the profile's speeds (applied)."
        )

    def test_a_wish_handed_in_by_a_caller_is_never_kept(self, tmp_path):
        gcode, wish = self._sliced_here(tmp_path)
        forged = {"ranges": [{"from_mm": 0, "to_mm": 9, "asked": {"speed_mm_s": 1}, "outcome": "applied"}]}
        assert sbh.keep_wish({sbh.RECORD_KEY: forged}, gcode)[sbh.RECORD_KEY] == wish
        other = _synthetic(tmp_path / "other.gcode", dict.fromkeys(_layers(0.2, 4.0), 60.0))
        assert sbh.keep_wish({sbh.RECORD_KEY: forged, "material": "PLA"}, other) == {"material": "PLA"}
        assert sbh.keep_wish({sbh.RECORD_KEY: forged}, None) is None

    def test_a_saved_version_keeps_and_says_it_and_never_a_callers(self, tmp_path, monkeypatch):
        from kiln.plugins.version_tools import _VersionToolsPlugin

        # An install without kiln-pro, so what is asserted is public's own answer.
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        monkeypatch.setattr("kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path / "designs"))
        tools = _plugin_tools(_VersionToolsPlugin())
        gcode, wish = self._sliced_here(tmp_path)
        forged = {"ranges": [{"from_mm": 0, "to_mm": 9, "asked": {"speed_mm_s": 1}, "outcome": "applied"}]}

        saved = tools["save_design_version"](
            design_id="post", scad_source="cube(10);", gcode_path=gcode, provenance={sbh.RECORD_KEY: forged},
        )
        assert saved["ok"] is True, saved
        assert saved["version"]["provenance"][sbh.RECORD_KEY] == wish
        said = "Sliced with its own speeds by height: 0 mm to 2 mm at 50% of the profile's speeds (applied)."
        assert saved["version"]["sliced_speeds"] == said
        assert tools["get_design_version"](version_id="post:1")["version"]["sliced_speeds"] == said

        unsliced = tools["save_design_version"](
            design_id="post", scad_source="cube(12);", provenance={sbh.RECORD_KEY: forged},
        )
        assert sbh.RECORD_KEY not in (unsliced["version"]["provenance"] or {})
        assert "sliced_speeds" not in unsliced["version"]

    def test_a_new_version_never_carries_its_parents_wish(self):
        from kiln.design_recipe import VERSION_STORY_KEYS

        assert sbh.RECORD_KEY in VERSION_STORY_KEYS

    def test_the_slice_notes_its_wish_on_its_own_record(self, tmp_path):
        from kiln import print_lineage

        plain = dict.fromkeys(_layers(0.2, 10.0), 60.0)
        made = {z: (20.0 if 2.0 < z <= 6.0 else 60.0) for z in plain}
        fake = _FakeSlicer(tmp_path, plain, made)
        real_call = fake.__call__

        def _run(out_path, **kwargs):
            result = real_call(out_path, **kwargs)
            if kwargs["record"]:
                print_lineage.record_slice(fake.part, out_path)
            return result

        out = tmp_path / "out"
        out.mkdir()
        result = sbh.sliced(
            sbh.checked([{"from_mm": 2, "to_mm": 6, "speed_mm_s": 20}]),
            family=sbh.PRUSA, out_file=str(out / "part.gcode"), run=_run,
        )
        print_lineage.flush()
        kept = print_lineage.speed_by_height_of(result.output_path)
        assert kept == sbh.kept_form(result.speed_by_height)
        assert kept["ranges"] == [{"from_mm": 2.0, "to_mm": 6.0, "asked": {"speed_mm_s": 20.0},
                                   "outcome": "applied", "fastest_mm_s": 20.0}]


# ---------------------------------------------------------------------------
# Every slice door hands the wish on
# ---------------------------------------------------------------------------

_WISH = [{"from_mm": 0, "to_mm": 2, "percent": 50}]


class _Stopped(SlicerError):
    """Raised by the stand-in slicer once it has seen what it was handed."""


def _seen_by_the_slicer() -> tuple[Any, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def _slice(path: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        raise _Stopped("stopped after reading the keywords")

    return _slice, seen


def _plugin_tools(plugin: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *args: Any, **kwargs: Any) -> Any:
            def deco(fn: Any) -> Any:
                captured[kwargs.get("name", fn.__name__)] = fn
                return fn

            return deco

    plugin.register(_Mcp())
    return captured


class TestEveryDoorHandsTheWishToTheSlice:
    def test_owner_asks_carries_it_only_when_given(self):
        assert owner_asks(speed_by_height=_WISH) == {"speed_by_height": _WISH}
        assert owner_asks() == {} and owner_asks(speed_by_height=None) == {}

    @pytest.mark.parametrize("tool", ["slice_model", "slice_and_estimate", "slice_and_print"])
    def test_the_slice_tools(self, tmp_path, tool):
        from kiln.plugins import estimate_tools, slicer_tools

        door = {**_plugin_tools(slicer_tools.plugin), **_plugin_tools(estimate_tools.plugin)}[tool]
        part = _stepped_part(tmp_path / "part.stl")
        slicer, seen = _seen_by_the_slicer()
        with patch("kiln.server._check_auth", return_value=None), patch("kiln.slicer.slice_file", slicer):
            reply = door(input_path=part, printer_id="bambu_a1", speed_by_height=_WISH)
        assert seen and seen[0].get("speed_by_height") == _WISH, reply
        seen.clear()
        with patch("kiln.server._check_auth", return_value=None), patch("kiln.slicer.slice_file", slicer):
            door(input_path=part, printer_id="bambu_a1")
        assert seen and "speed_by_height" not in seen[0]

    def test_the_command_line(self, tmp_path):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        part = _stepped_part(tmp_path / "part.stl")
        slicer, seen = _seen_by_the_slicer()
        with patch("kiln.slicer.slice_file", slicer):
            CliRunner().invoke(cli, ["slice", part, "--speed-by-height", json.dumps(_WISH), "--json"])
        assert seen and seen[0].get("speed_by_height") == json.dumps(_WISH)

    def test_a_rebuild(self, tmp_path):
        from kiln.design_recipe import DesignPart, DesignRecipe, save_recipe
        from kiln.plugins.design_rebuild_tools import plugin

        design = tmp_path / "design"
        design.mkdir()
        stl = _stepped_part(design / "part0.stl")
        save_recipe(DesignRecipe(
            name="one-part", created="2026-08-17T00:00:00+00:00",
            parts=[DesignPart(name="part0", role="structural", stl_path=stl, color="white", filament_slot=0)],
            merge_order=["part0"],
        ), str(design))
        slicer, seen = _seen_by_the_slicer()
        with patch("kiln.slicer.slice_file", slicer):
            _plugin_tools(plugin)["rebuild_design"](str(design), speed_by_height=_WISH)
        assert seen and seen[0].get("speed_by_height") == _WISH

    def test_the_design_pipeline(self, tmp_path):
        import kiln.server as server
        from kiln.design_reasoning import DesignToGCodeResult

        handed: dict[str, Any] = {}

        def _pipeline(description: str, **kwargs: Any) -> Any:
            handed.update(kwargs)
            return DesignToGCodeResult(success=False, description=description)

        with patch("kiln.server._check_auth", return_value=None), patch("kiln.design_reasoning.design_to_gcode", _pipeline):
            server.design_to_gcode_pipeline("a small bracket", output_dir=str(tmp_path), speed_by_height=_WISH)
        assert handed["slice_asks"] == {"speed_by_height": _WISH}

        from kiln.design_reasoning import design_to_gcode

        slicer, seen = _seen_by_the_slicer()
        with patch("kiln.slicer.slice_file", slicer):
            design_to_gcode("a simple box 40 mm wide", output_dir=str(tmp_path), slice_asks=handed["slice_asks"])
        if not seen:
            pytest.skip("the pipeline made no part to slice on this machine")
        assert seen[0].get("speed_by_height") == _WISH


# ---------------------------------------------------------------------------
# The real slicers, through the door a slice really goes through
# ---------------------------------------------------------------------------


def _find(names: tuple[str, ...], mac: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return mac if os.path.isfile(mac) and os.access(mac, os.X_OK) else None


_SLICERS = {
    "PrusaSlicer": _find(("prusa-slicer", "PrusaSlicer", "prusaslicer"), "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"),
    "OrcaSlicer": _find(("orca-slicer", "OrcaSlicer", "orcaslicer"), "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
    "Bambu Studio": _find(("bambu-studio", "BambuStudio", "bambustudio"), "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
}
_EVERY_SLICER = [
    pytest.param(name, marks=pytest.mark.skipif(path is None, reason=f"{name} not installed"))
    for name, path in _SLICERS.items()
]

_WORDS = re.compile(r"([XYZEF])(-?\d*\.?\d+)")
_NOT_THE_PART = re.compile(r"skirt|brim|tower|custom|support", re.IGNORECASE)


def _part_moves(gcode_path: str) -> list[tuple[float, float, float, float]]:
    """``(layer z, mm/s, length mm, mm³/s)`` of every extruding move of the
    part in a sliced file, read here from the bytes: the layer is the one
    the slicer's own layer line names."""
    moves: list[tuple[float, float, float, float]] = []
    relative = False
    x = y = e = feed = 0.0
    layer_z: float | None = None
    kind = "custom"
    with open(gcode_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith(";"):
                at = re.match(r";\s*(?:Z|Z_HEIGHT)\s*:\s*([\d.]+)\s*$", line)
                if at:
                    layer_z = round(float(at.group(1)), 3)
                named = re.match(r";\s*(?:TYPE|FEATURE)\s*:\s*(.+)$", line)
                if named:
                    kind = named.group(1)
                continue
            code = line.split(";", 1)[0]
            if code.startswith("M83"):
                relative = True
            elif code.startswith("M82"):
                relative = False
            elif code.startswith("G92"):
                e = dict((k, float(v)) for k, v in _WORDS.findall(code)).get("E", e)
            if not re.match(r"G[0123]\b", code):
                continue
            words = dict((k, float(v)) for k, v in _WORDS.findall(code[2:]))
            feed = words.get("F", feed)
            nx, ny = words.get("X", x), words.get("Y", y)
            pushed = 0.0
            if "E" in words:
                pushed = words["E"] if relative else words["E"] - e
                e = e if relative else words["E"]
            length = math.hypot(nx - x, ny - y)
            x, y = nx, ny
            if pushed > 1e-6 and length >= 0.5 and layer_z is not None and not _NOT_THE_PART.search(kind):
                moves.append((layer_z, feed / 60.0, length, pushed / length * _FILAMENT_AREA * feed / 60.0))
    return moves


def _flow_limit(gcode_path: str) -> float:
    text = Path(gcode_path).read_text(encoding="utf-8", errors="replace")
    limits = [
        float(found.group(1)) for key in ("filament_max_volumetric_speed", "max_volumetric_speed")
        if (found := re.search(rf"^; {key} = ([\d.]+)", text, re.MULTILINE)) and float(found.group(1)) > 0
    ]
    assert limits, "the sliced file states no flow limit"
    return min(limits)


@pytest.mark.parametrize("slicer_name", _EVERY_SLICER)
class TestTheRealSlicers:
    """A stepped part through :func:`kiln.slicer.slice_file` with the
    bundled Bambu Lab A1 profile: slow from the plate to 2 mm, as fast as
    asked from 2 mm to 3 mm, and the post above left alone."""

    WISH = [{"from_mm": 0, "to_mm": 2, "speed_mm_s": 12}, {"from_mm": 2, "to_mm": 3, "speed_mm_s": 300}]

    def _slice(self, tmp_path: Path, slicer_name: str, name: str, **kwargs: Any) -> SliceResult:
        from kiln.slicer_profiles import resolve_slicer_profile

        part = tmp_path / "part.stl"
        if not part.exists():
            _stepped_part(part)
        return slice_file(
            str(part), output_dir=str(tmp_path / name), profile=resolve_slicer_profile("bambu_a1"),
            slicer_path=_SLICERS[slicer_name], material="PLA", timeout=600, **kwargs,
        )

    def test_the_file_prints_each_range_as_asked_and_the_rest_as_before(self, tmp_path, slicer_name):
        from kiln.gcode import continues_a_print
        from kiln.print_lineage import _load, flush, speed_by_height_of
        from kiln.slicer_estimate import read_estimate

        on_record = len(_load()["records"])
        plain = self._slice(tmp_path, slicer_name, "plain")
        made = self._slice(tmp_path, slicer_name, "wish", speed_by_height=self.WISH)
        before, after = _part_moves(plain.output_path), _part_moves(made.output_path)
        heights = sorted({z for z, *_ in before})
        assert heights == sorted({z for z, *_ in after})
        middle = {z: z - (z - (heights[i - 1] if i else 0.0)) / 2.0 for i, z in enumerate(heights)}

        slow = [m for m in after if middle[m[0]] < 2.0]
        fast = [m for m in after if 2.0 <= middle[m[0]] < 3.0]
        fast_before = [m for m in before if 2.0 <= middle[m[0]] < 3.0]
        assert slow and fast
        # Slow from the plate: no line of the part, the first layer's included, above 12 mm/s.
        assert max(speed for _z, speed, _l, _f in slow) <= 12.0 + 0.01
        assert min(z for z, *_ in slow) == heights[0]
        assert max(speed for _z, speed, _l, _f in [m for m in before if middle[m[0]] < 2.0]) > 30.0
        # Fast above it: 300 mm/s, or as near it as the nozzle can melt, and never past that.
        limit = _flow_limit(made.output_path)
        fastest = max(m[1] for m in fast)
        assert fastest >= max(m[1] for m in fast_before) - 0.01
        assert max(m[3] for m in after) <= limit * 1.03
        if fastest < 300.0 - 0.5:
            assert max(m[3] for m in fast) >= limit * 0.95
        # The post above 3 mm prints as it does without the wish: layer by layer the same
        # length of line, at the same speeds (a slicer re-times a short layer for cooling
        # from one slice to the next, by under 3% here).
        def _by_layer(moves: list[tuple[float, float, float, float]]) -> dict[float, tuple[float, float, float]]:
            layers: dict[float, list[float]] = {}
            for z, speed, length, _flow in moves:
                if middle[z] >= 3.0:
                    total = layers.setdefault(z, [0.0, 0.0, 0.0])
                    total[0] += length
                    total[1] = max(total[1], speed)
                    total[2] += length / speed
            return {z: (total[0], total[1], total[0] / total[2]) for z, total in layers.items()}

        above, above_before = _by_layer(after), _by_layer(before)
        assert len(above) >= 20 and sorted(above) == sorted(above_before)
        for z, (length, fastest_here, typical) in above.items():
            length_before, fastest_before, typical_before = above_before[z]
            assert length == pytest.approx(length_before, rel=0.005), z
            assert fastest_here == pytest.approx(fastest_before, rel=0.03), z
            assert typical == pytest.approx(typical_before, rel=0.03), z

        block = made.speed_by_height
        assert block["ranges"][0]["outcome"] == sbh.APPLIED
        assert block["ranges"][1]["outcome"] == (sbh.CAPPED if fastest < 300.0 - 0.5 else sbh.APPLIED)
        assert block["ranges"][0]["measured"]["fastest_mm_s"] == pytest.approx(12.0, abs=0.05)
        assert block["ranges"][1]["measured"]["fastest_mm_s"] == pytest.approx(fastest, abs=0.06)
        assert block["ranges"][1]["measured"]["peak_flow_mm3_s"] <= limit * 1.03
        assert block["outside_ranges"] == "unchanged"
        # The slicer's own time moved with the wish, and both times are the files' own.
        assert block["estimate_seconds"] == read_estimate(made.output_path)["seconds"]
        # (The slice without the wish is one Kiln made itself, of the part placed as a slicer places it.)
        assert block["estimate_without_seconds"] == pytest.approx(read_estimate(plain.output_path)["seconds"], abs=5)
        assert block["estimate_seconds"] > block["estimate_without_seconds"]
        assert made.to_dict()["speed_by_height"] == block and plain.speed_by_height is None

        # The file is a slice like any other: no speed or flow command in it,
        # it carries no earlier print on, and Kiln's record of it keeps the wish.
        text = Path(made.output_path).read_text(encoding="utf-8", errors="replace")
        assert not re.search(r"^\s*M22[01]\b", text, re.MULTILINE)
        assert not continues_a_print(text)
        flush()
        assert speed_by_height_of(made.output_path) == sbh.kept_form(block)
        assert speed_by_height_of(plain.output_path) is None
        # Two slices were asked for and two are on record: the slice Kiln made
        # to compare against left none.
        assert len(_load()["records"]) == on_record + 2

    def test_ranges_that_name_another_object_are_caught_and_the_slice_refused(self, tmp_path, slicer_name):
        real = sbh._ranges_xml

        def _another_object(plan, objects, top_mm):
            return real(plan, objects, top_mm).replace('<object id="1">', '<object id="2">')

        with patch.object(sbh, "_ranges_xml", _another_object), pytest.raises(sbh.SpeedByHeightError) as refused:
            self._slice(tmp_path, slicer_name, "wish", speed_by_height=self.WISH)
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED"
        assert not list((tmp_path / "wish").glob("*.gcode"))

    def test_a_range_above_the_part_is_refused(self, tmp_path, slicer_name):
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            self._slice(tmp_path, slicer_name, "wish", speed_by_height=[{"from_mm": 20, "to_mm": 30, "percent": 50}])
        assert refused.value.code == "SPEED_BY_HEIGHT_OUTSIDE_PART"
        assert not list((tmp_path / "wish").glob("*.gcode"))
