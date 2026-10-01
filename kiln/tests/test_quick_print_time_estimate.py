"""The no-slicer print-time estimate answers in the right order of magnitude.

2026-10-01: ``estimate_mesh_print_time`` said 7 minutes for an enclosure the
slicer timed at over an hour.  The estimator took the square root of a
length for a layer's path and counted one pass of it per layer; compared
with 36 real slices it answered a median of 9% of the sliced time.  The
validation pipeline's estimate step, meanwhile, read two keys the estimator
never returned and reported every part as 0 minutes and 0 grams — its test
handed it a mock with those keys in it.
"""

from __future__ import annotations

import struct

import pytest

from kiln.generation.validation import (
    EST_TIME_RANGE,
    deposited_volume_mm3,
    estimate_print_time_from_mesh,
)


def _stl(path, triangles) -> str:
    data = bytearray(b"\x00" * 80) + struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        data += struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0)
    path.write_bytes(bytes(data))
    return str(path)


def _box(path, x: float, y: float, z: float, *, lid: bool = True) -> str:
    """A box with its corner at the origin; ``lid=False`` leaves the top open."""
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    faces = [
        (0, 2, 1), (0, 3, 2), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    if lid:
        faces += [(4, 5, 6), (4, 6, 7)]
    return _stl(path, [(v[a], v[b], v[c]) for a, b, c in faces])


class TestTheEstimateIsInTheRightPlace:
    def test_a_20mm_cube_lands_where_its_slices_did(self, tmp_path):
        """Sliced on seven printer profiles, this cube took 674 to 1400 s.
        The old formula answered 224 s."""
        got = estimate_print_time_from_mesh(_box(tmp_path / "cube.stl", 20, 20, 20))
        assert 674 <= got["estimated_time_seconds"] <= 1400, got["estimated_time_seconds"]

    def test_the_figure_is_the_stated_model(self, tmp_path):
        """Plastic over flow, plus what a layer costs besides extrusion."""
        got = estimate_print_time_from_mesh(_box(tmp_path / "cube.stl", 20, 20, 20))
        plastic = 2400 * 1.2 + (8000 - 2400 * 1.2) * 0.20        # shell + 20% of the rest
        flow = 60.0 * 0.2 * 0.4                                  # mm^3 per second
        assert got["plastic_volume_mm3"] == pytest.approx(plastic, rel=1e-3)
        assert got["layers"] == 100
        assert got["estimated_time_seconds"] == pytest.approx(100 * (plastic / 100 / flow + 3.0), rel=1e-3)

    def test_twice_the_plastic_at_one_height_takes_about_twice_as_long(self, tmp_path):
        """The check the square-root formula failed: it grew with the square
        root of the surface, so a part four times the size took twice as long."""
        small = estimate_print_time_from_mesh(_box(tmp_path / "a.stl", 40, 40, 20))
        big = estimate_print_time_from_mesh(_box(tmp_path / "b.stl", 80, 80, 20))
        extrusion = lambda r: r["estimated_time_seconds"] - r["layers"] * 3.0  # noqa: E731
        assert extrusion(big) / extrusion(small) == pytest.approx(
            big["plastic_volume_mm3"] / small["plastic_volume_mm3"], rel=1e-3
        )
        assert big["estimated_time_seconds"] > 2.5 * small["estimated_time_seconds"]

    def test_a_faster_printer_is_a_shorter_print(self, tmp_path):
        part = _box(tmp_path / "cube.stl", 40, 40, 40)
        slow = estimate_print_time_from_mesh(part, print_speed_mm_s=40)
        fast = estimate_print_time_from_mesh(part, print_speed_mm_s=120)
        assert fast["estimated_time_seconds"] < slow["estimated_time_seconds"]

    def test_a_thin_tall_part_is_held_to_the_layer_time_floor(self, tmp_path):
        """A small layer is not printed faster than it can cool: the sliced
        80 mm rod ran 5.6 s a layer on the fastest profile."""
        got = estimate_print_time_from_mesh(_box(tmp_path / "pin.stl", 4, 4, 80))
        assert got["estimated_time_seconds"] == pytest.approx(got["layers"] * 5.5)


class TestItSaysHowRoughItIs:
    def test_the_range_is_the_measured_one_and_brackets_the_figure(self, tmp_path):
        got = estimate_print_time_from_mesh(_box(tmp_path / "cube.stl", 20, 20, 20))
        low, high = got["range_seconds"]
        assert low == pytest.approx(got["estimated_time_seconds"] * EST_TIME_RANGE[0], abs=0.1)
        assert high == pytest.approx(got["estimated_time_seconds"] * EST_TIME_RANGE[1], abs=0.1)
        assert low < got["estimated_time_seconds"] < high
        assert "Slice the part" in got["note"] and " to " in got["range_human"]

    def test_the_assumptions_ride_the_result(self, tmp_path):
        got = estimate_print_time_from_mesh(_box(tmp_path / "cube.stl", 20, 20, 20))
        assert "3 walls" in got["assumptions"] and "20% infill" in got["assumptions"]

    def test_an_open_surface_is_refused_not_guessed(self, tmp_path):
        with pytest.raises(ValueError, match="encloses no volume"):
            estimate_print_time_from_mesh(_stl(
                tmp_path / "sheet.stl",
                [((0, 0, 0), (10, 0, 0), (0, 0, 10)), ((10, 0, 0), (10, 0, 10), (0, 0, 10))],
            ))


class TestDepositedPlastic:
    def test_a_thin_part_never_deposits_more_than_it_contains(self):
        """A 1 mm plate is all wall.  Its two faces counted separately at
        1.2 mm each came to 2.4 times the plate's own volume."""
        shell, infill = deposited_volume_mm3(10_000, 20_400)
        assert shell + infill == pytest.approx(10_000)

    def test_a_chunky_part_is_shell_plus_a_fifth_of_the_rest(self):
        shell, infill = deposited_volume_mm3(96_000, 12_800)
        assert shell == pytest.approx(15_360) and infill == pytest.approx(16_128)

    def test_the_cost_estimate_is_about_the_same_print(self, tmp_path):
        """One statement of the plastic, read by both: a part's weight and
        its time are never estimates of two different prints."""
        from kiln.cost_estimator import CostEstimator

        part = _box(tmp_path / "plate.stl", 100, 100, 1)
        cost = CostEstimator().estimate_from_mesh(part).to_dict()
        time = estimate_print_time_from_mesh(part)
        assert cost["total_plastic_volume_mm3"] == pytest.approx(10_000, rel=1e-3)
        assert time["plastic_volume_mm3"] == pytest.approx(cost["total_plastic_volume_mm3"], rel=1e-3)


class TestThePipelineReadsWhatTheEstimatorReturns:
    def test_the_estimate_step_reports_real_minutes_and_grams(self, tmp_path):
        """Through the real estimator, not a mock that agrees with the caller."""
        from kiln.plugins import _validation_pipeline_internals as internals

        report = internals._PipelineReport()
        internals._step_estimate(report, _box(tmp_path / "cube.stl", 20, 20, 20))
        info = report.model_info
        assert info["estimated_print_time_min"] >= 11      # the fastest slice was 674 s
        assert info["estimated_filament_g"] > 3
        assert info.get("estimate_source") != "bounding_box"
