"""The wall check: critical below the wall that closes, a warning below the
recommended one.

A wall thinner than the thinnest wall the plastic closes at all will not
print as a closed shell, so it is critical.  A wall between that and the
plastic's recommended wall prints, and is thinner than recommended for
strength, so it is a warning.  A brief's own minimum wall keeps its rule: a
wall under half of it is critical.
"""

from __future__ import annotations

from kiln.design_validator import _check_wall_thickness, _resolve_wall_thickness, _wall_floor

#: ABS's limits as the design-limits helper hands them to the validator.
_ABS = {"min_wall_thickness_mm": 0.8, "recommended_wall_thickness_mm": 2.0}


def test_below_the_wall_that_closes_is_critical():
    check = _check_wall_thickness(0.7, 2.0, floor_mm=0.8)
    assert not check.passed and check.severity == "critical"


def test_between_the_floor_and_the_recommended_wall_is_a_warning():
    check = _check_wall_thickness(0.9, 2.0, floor_mm=0.8)
    assert not check.passed and check.severity == "warning"


def test_the_required_wall_is_the_recommended_one():
    assert _resolve_wall_thickness({}, _ABS) == 2.0
    assert _wall_floor({}, _ABS) == 0.8


def test_a_brief_minimum_stricter_than_the_material_wins():
    assert _resolve_wall_thickness({"min_wall_thickness_mm": 3.0}, _ABS) == 3.0


def test_without_a_floor_half_the_required_wall_is_critical():
    assert _check_wall_thickness(0.9, 2.0).severity == "critical"
    assert _check_wall_thickness(1.1, 2.0).severity == "warning"
