"""More a height range can ask for: a most speed, the inside lines only, a
gentler acceleration; and ranges chosen by a planner after the plain slice.

Every outcome is read from a sliced file written here, the way
:func:`kiln.speed_by_height.sliced` reads the slicer's.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

import pytest

from kiln import speed_by_height as sbh
from kiln.slicer import SliceResult

_AREA = math.pi * (1.75 / 2.0) ** 2
OUTER, INNER, INFILL = "External perimeter", "Perimeter", "Internal infill"


def _part(path: Path) -> str:
    v = [(x, y, z) for x in (0, 30) for y in (0, 30) for z in (0, 10)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0))
    return str(path)


def _gcode(path: Path, layers: dict[float, dict[str, Any]], *, seconds: int = 600) -> str:
    """One 40 mm line of each kind a layer; a layer is ``{kind: mm/s}`` plus
    ``"accel"``: one acceleration for the layer, or ``{kind: acceleration}``."""
    lines = ["M83", "G1 Z5 F6000"]
    for z, layer in sorted(layers.items()):
        lines += [";LAYER_CHANGE", f";Z:{z:g}", ";HEIGHT:0.2", f"G1 Z{z:g} F6000"]
        accel = layer.get("accel")
        for kind in (INNER, OUTER, INFILL):
            if kind not in layer:
                continue
            lines += [f";TYPE:{kind}", "G1 X0 Y0 F9000"]
            commanded = accel.get(kind) if isinstance(accel, dict) else accel
            if commanded:
                lines.append(f"M204 S{commanded:g}")
            speed = layer[kind]
            lines.append(f"G1 X40 Y0 E{2.0 / speed / _AREA * 40:.5f} F{speed * 60:g}")
    hours, rest = divmod(seconds, 3600)
    lines.append(f"; estimated printing time (normal mode) = {hours}h {rest // 60}m {rest % 60}s")
    lines += ["; filament_diameter = 1.75", "; layer_height = 0.2", "; perimeter_speed = 60",
              "; external_perimeter_speed = 40", "; infill_speed = 100", "; max_volumetric_speed = 11"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _zs(first: float, last: float) -> list[float]:
    return [round(first + 0.2 * i, 3) for i in range(int(round((last - first) / 0.2)) + 1)]


PLAIN = {z: {INNER: 60.0, OUTER: 40.0, INFILL: 100.0, "accel": 6000} for z in _zs(0.2, 10.0)}


class _Slicer:
    def __init__(self, tmp_path: Path, made: dict[float, dict[str, Any]], plain: dict[float, dict[str, Any]] = PLAIN):
        self.tmp_path, self.made, self.plain = tmp_path, made, plain
        self.part = _part(tmp_path / "part.stl")
        self.options: list[str] = []
        self.calls = 0

    def __call__(self, out_path: str, *, prepare: Any, first_layer_speed: float | None = None, record: bool) -> SliceResult:
        self.calls += 1
        handed = self.tmp_path / f"handed_{self.calls}"
        handed.mkdir()
        import zipfile

        with zipfile.ZipFile(prepare(self.part, str(handed), (100.0, 100.0))) as archive:
            self.options.append("".join(
                archive.read(name).decode() for name in archive.namelist() if "layer_config_ranges" in name
            ))
        _gcode(Path(out_path), self.made if record else self.plain, seconds=700 if record else 600)
        return SliceResult(success=True, output_path=out_path, slicer="fake", message="Sliced.")


def _sliced(tmp_path: Path, made, wish=None, **kwargs: Any):
    fake = _Slicer(tmp_path, made)
    out = tmp_path / "out" / "part.gcode"
    out.parent.mkdir(exist_ok=True)
    family = kwargs.pop("family", sbh.PRUSA)
    return fake, sbh.sliced(sbh.checked(wish), family=family, out_file=str(out), run=fake, **kwargs)


def _with(changes: dict[str, Any], first: float, last: float) -> dict[float, dict[str, Any]]:
    return {z: ({**layer, **changes} if first < z <= last + 1e-9 else layer) for z, layer in PLAIN.items()}


class TestTheWishItself:
    @pytest.mark.parametrize(("wish", "says"), [
        ([{"from_mm": 0, "to_mm": 4, "max_mm_s": 30, "percent": 50}], "needs one speed"),
        ([{"from_mm": 0, "to_mm": 4, "max_mm_s": 5000}], "max_mm_s is from 1 to 1000"),
        ([{"from_mm": 0, "to_mm": 4, "percent": 120, "lines": "walls"}], 'lines is "all" or "inside"'),
        ([{"from_mm": 0, "to_mm": 4, "max_mm_s": 30, "lines": "inside"}], "goes with speed_mm_s or percent"),
        ([{"from_mm": 0, "to_mm": 4, "max_acceleration_mm_s2": 5}], "max_acceleration_mm_s2 is from 100 to 50000"),
    ])
    def test_a_range_that_cannot_be_printed_as_said_is_refused(self, wish, says):
        with pytest.raises(sbh.SpeedByHeightError, match=says):
            sbh.checked(wish)

    def test_a_range_may_ask_only_for_gentler_acceleration(self):
        (band,) = sbh.checked([{"from_mm": 2, "to_mm": 4, "max_acceleration_mm_s2": 1500}])
        assert band.asked() == {"max_acceleration_mm_s2": 1500.0} and not band.speed_asked()

    def test_what_was_asked_is_kept_and_said_in_words(self):
        (band,) = sbh.checked([{"from_mm": 2, "max_mm_s": 60, "max_acceleration_mm_s2": 2000}])
        assert band.asked() == {"max_mm_s": 60.0, "max_acceleration_mm_s2": 2000.0}
        assert band.wanted() == "no line faster than 60 mm/s, speeding up and stopping at no more than 2000 mm/s²"


class TestAMostSpeed:
    WISH = [{"from_mm": 4, "to_mm": 8, "max_mm_s": 50}]

    def test_only_the_kinds_faster_than_the_most_are_asked_down(self, tmp_path):
        fake, result = _sliced(tmp_path, _with({INNER: 50.0, INFILL: 50.0}, 4, 8), self.WISH)
        asked = fake.options[-1]
        assert 'opt_key="perimeter_speed">50<' in asked and 'opt_key="infill_speed">50<' in asked
        assert "external_perimeter_speed" not in asked            # 40 mm/s is under 50: left as it is
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.APPLIED and band["measured"]["fastest_mm_s"] == 50.0

    def test_lines_the_slicer_left_faster_than_the_most_refuse_the_slice(self, tmp_path):
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _sliced(tmp_path, PLAIN, self.WISH)
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED" and not (tmp_path / "out" / "part.gcode").exists()

    def test_a_stretch_already_under_the_most_says_nothing_changed(self, tmp_path):
        _fake, result = _sliced(tmp_path, PLAIN, [{"from_mm": 4, "to_mm": 8, "max_mm_s": 150}])
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.ALREADY_SLOWER and "nothing changed" in band["said"]


class TestTheInsideLines:
    WISH = [{"from_mm": 4, "to_mm": 8, "percent": 150, "lines": "inside"}]

    def test_only_the_infill_and_inner_walls_are_asked_for_more(self, tmp_path):
        fake, result = _sliced(tmp_path, _with({INNER: 90.0, INFILL: 150.0}, 4, 8), self.WISH)
        asked = fake.options[-1]
        assert 'opt_key="perimeter_speed">90<' in asked and 'opt_key="infill_speed">150<' in asked
        assert "external_perimeter_speed" not in asked
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.APPLIED and band["measured"]["fastest_mm_s"] == 150.0

    def test_the_outcome_is_judged_on_the_inside_lines_alone(self, tmp_path):
        # The outer wall sped up and the inside lines did not: not applied.
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _sliced(tmp_path, _with({OUTER: 150.0}, 4, 8), self.WISH)
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED"


class TestAGentlerAcceleration:
    WISH = [{"from_mm": 4, "to_mm": 8, "max_acceleration_mm_s2": 2000}]

    def test_a_slicer_that_cannot_hold_it_by_height_refuses_before_slicing_with_it(self, tmp_path):
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _sliced(tmp_path, PLAIN, self.WISH, family=sbh.BAMBU)
        assert refused.value.code == "SPEED_BY_HEIGHT_NO_ACCELERATION" and "Bambu Studio can" in str(refused.value)

    def test_held_on_every_line_is_applied_and_says_the_most(self, tmp_path):
        fake, result = _sliced(tmp_path, _with({"accel": 2000}, 4, 8), self.WISH, family=sbh.BAMBU, eases_acceleration=True)
        assert 'opt_key="slowdown_start_acc">2000<' in fake.options[-1] and 'opt_key="enable_height_slowdown">1<' in fake.options[-1]
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.APPLIED and band["acceleration"]["held_share"] == 1.0
        assert "no more than 2000 mm/s² here" in band["said"]

    def test_held_on_most_lines_says_the_share_and_what_the_rest_runs_at(self, tmp_path):
        made = _with({"accel": {INNER: 5000, OUTER: 2000, INFILL: 2000}}, 4, 8)
        _fake, result = _sliced(tmp_path, made, self.WISH, family=sbh.BAMBU, eases_acceleration=True)
        (band,) = result.speed_by_height["ranges"]
        assert band["outcome"] == sbh.APPLIED and band["acceleration"] == {
            "asked_mm_s2": 2000.0, "hardest_mm_s2": 5000.0, "held_share": pytest.approx(0.667, abs=0.001),
        }
        assert "for 67% of the printing here" in band["said"] and "up to 5000 mm/s²" in band["said"]

    def test_not_held_on_most_lines_refuses_the_slice(self, tmp_path):
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            _sliced(tmp_path, PLAIN, self.WISH, family=sbh.BAMBU, eases_acceleration=True)
        assert refused.value.code == "SPEED_BY_HEIGHT_NOT_APPLIED" and "harder than asked" in str(refused.value)


class TestRangesAPlannerChose:
    def _planner(self, ranges, said=("One line about the whole part.",), ask=None):
        seen = []

        def plan(plain):
            seen.append(plain)
            return {"ranges": ranges, "said": list(said), "ask": ask}

        return plan, seen

    def test_the_planner_is_handed_the_plain_slice_and_its_ranges_are_sliced_in(self, tmp_path):
        plan, seen = self._planner([{"from_mm": 4, "to_mm": 8, "max_mm_s": 50, "why": "Gentle. It can sway."}],
                                   ask={"about": "carries_load", "question": "Does it carry load?"})
        _fake, result = _sliced(tmp_path, _with({INNER: 50.0, INFILL: 50.0}, 4, 8), planner=plan)
        assert len(seen) == 1 and seen[0].layers and seen[0].seconds == 600
        block = result.speed_by_height
        assert block["picked_by"] == sbh.PICKED_BY_KILN and block["notes"] == ["One line about the whole part."]
        assert block["ask"]["question"] == "Does it carry load?"
        assert block["ranges"][0]["why"] == "Gentle. It can sway."
        assert block["headline"] == "Kiln prints 1 stretch gently; the print is about 1m longer."
        assert block["said"].startswith(block["headline"] + " 4 mm to 8 mm: Gentle. It can sway. In the sliced file: applied.")
        assert block["said"].endswith("One line about the whole part. Does it carry load?")

    def test_a_chosen_range_starts_and_ends_where_a_layer_does(self, tmp_path):
        plan, _seen = self._planner([{"from_mm": 3.93, "to_mm": 8.07, "max_mm_s": 50, "why": "Gentle."}])
        fake, result = _sliced(tmp_path, _with({INNER: 50.0, INFILL: 50.0}, 4, 8), planner=plan)
        assert 'min_z="4" max_z="8"' in fake.options[-1]
        assert (result.speed_by_height["ranges"][0]["from_mm"], result.speed_by_height["ranges"][0]["to_mm"]) == (4.0, 8.0)

    def test_no_ranges_slices_the_part_as_it_was_and_says_what_the_planner_said(self, tmp_path):
        plan, _seen = self._planner([], said=("This part is steady all the way up.",))
        fake, result = _sliced(tmp_path, PLAIN, planner=plan)
        block = result.speed_by_height
        assert block["ranges"] == [] and block["picked_by"] == sbh.PICKED_BY_KILN
        assert block["said"] == "Kiln changed no speeds for this part. This part is steady all the way up."
        assert fake.options[-1] == ""
        assert block["estimate_seconds"] == block["estimate_without_seconds"] == 600

    def test_the_record_keeps_who_chose_and_the_sentence_says_so(self, tmp_path):
        plan, _seen = self._planner([{"from_mm": 4, "to_mm": 8, "max_mm_s": 50, "why": "Gentle."}])
        _fake, result = _sliced(tmp_path, _with({INNER: 50.0, INFILL: 50.0}, 4, 8), planner=plan)
        kept = sbh.kept_form(result.speed_by_height)
        assert kept["picked_by"] == sbh.PICKED_BY_KILN
        assert sbh.wish_sentence({sbh.RECORD_KEY: kept}) == (
            "Sliced with speeds Kiln chose by height: 4 mm to 8 mm, no line faster than 50 mm/s (applied)."
        )

    def test_a_persons_own_ranges_are_never_marked_as_kilns(self, tmp_path):
        _fake, result = _sliced(tmp_path, _with({INNER: 50.0, INFILL: 50.0}, 4, 8), [{"from_mm": 4, "to_mm": 8, "max_mm_s": 50}])
        assert "picked_by" not in result.speed_by_height and "picked_by" not in sbh.kept_form(result.speed_by_height)
