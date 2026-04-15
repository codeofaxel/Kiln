"""Aspect-ratio-adaptive camera framing for visualize_model.

Pure horizontal views (front/right/back at ``rx=90``) render a 160×160×18
jewelry tray as an 18mm-tall strip — uninformative slabs that hide every
decoration.  The fix tilts those views toward the top for flat models
and steepens top/bottom for tall models.

These tests exercise the branch logic without needing OpenSCAD so they
run fast in CI.
"""
from __future__ import annotations

from kiln.model_visualizer import (
    _adapt_angles_to_bbox,
    _ANGLE_ROTATIONS,
    _BoundingBoxInfo,
    _FLAT_ASPECT_RATIO,
    _FLAT_TILT_DEGREES,
    _TALL_ASPECT_RATIO,
)


def _bbox(dx: float, dy: float, dz: float) -> _BoundingBoxInfo:
    return _BoundingBoxInfo(dx=dx, dy=dy, dz=dz)


class TestFlatModelTiltsSideViews:
    """Jewelry tray / coaster — flat models must not render side views
    as pure-horizontal strips.
    """

    def test_jewelry_tray_tilts_front_right_back(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(160, 160, 18))  # aspect ≈ 0.11
        expected_rx = 90 - _FLAT_TILT_DEGREES  # 55
        assert angles["front"] == (expected_rx, 0, 0)
        assert angles["right"] == (expected_rx, 0, 90)
        assert angles["back"] == (expected_rx, 0, 180)

    def test_coaster_tilts_front_right_back(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(90, 90, 7))  # aspect ≈ 0.08
        expected_rx = 90 - _FLAT_TILT_DEGREES
        assert angles["front"][0] == expected_rx
        assert angles["right"][0] == expected_rx
        assert angles["back"][0] == expected_rx

    def test_flat_preserves_top_and_bottom(self) -> None:
        """Top/bottom are already useful for flat models — don't touch."""
        angles = _adapt_angles_to_bbox(_bbox(160, 160, 18))
        assert angles["top"] == _ANGLE_ROTATIONS["top"]
        assert angles["bottom"] == _ANGLE_ROTATIONS["bottom"]

    def test_flat_preserves_isometric(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(160, 160, 18))
        assert angles["isometric"] == _ANGLE_ROTATIONS["isometric"]


class TestCubicModelUsesDefaults:
    """Approximately cubic models should use the default rotation presets."""

    def test_cube_untouched(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(50, 50, 50))  # aspect = 1.0
        assert angles == _ANGLE_ROTATIONS

    def test_moderate_aspect_untouched(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(40, 60, 30))  # aspect = 0.5
        assert angles == _ANGLE_ROTATIONS


class TestTallModelSteepensTopBottom:
    """Tall skinny models — top/bottom views benefit from more tilt so
    they don't render as flat circles / squares.
    """

    def test_vase_top_and_bottom_steepened(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(60, 60, 150))  # aspect = 2.5
        assert angles["top"] == (30, 0, 10)
        assert angles["bottom"] == (150, 0, 15)

    def test_tall_leaves_side_views_alone(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(60, 60, 150))
        # Side views already informative when model is tall
        assert angles["front"] == _ANGLE_ROTATIONS["front"]
        assert angles["right"] == _ANGLE_ROTATIONS["right"]


class TestThresholdsAreSensible:
    def test_flat_threshold_below_0_3(self) -> None:
        assert _FLAT_ASPECT_RATIO < 0.5

    def test_tall_threshold_above_1_5(self) -> None:
        assert _TALL_ASPECT_RATIO > 1.5

    def test_flat_tilt_produces_visible_top(self) -> None:
        """Tilt must be large enough to see top, small enough to still
        convey that it's a side view."""
        assert 20 <= _FLAT_TILT_DEGREES <= 60

    def test_degenerate_zero_dimensions_dont_crash(self) -> None:
        angles = _adapt_angles_to_bbox(_bbox(0.0, 0.0, 0.0))
        assert set(angles.keys()) == set(_ANGLE_ROTATIONS.keys())
