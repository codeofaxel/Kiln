"""The cost estimate prices the print it is asked about.

A downward face within two layers of the plate rests on it; one higher up
needs support.  Which faces those are depends on the layer the part prints
at, so the support plastic in a cost estimate follows the print's own layer
height, through both doors that price a mesh (the cost tool's estimator and
the printability report).  And a saving the report suggests compares that
same print with one setting changed, nothing else.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kiln import printability
from kiln.cost_estimator import CostEstimator

#: The ledge's underside sits this far above the plate: inside the plate
#: band at a 0.2 mm layer (0.4 mm), outside it at a 0.12 mm layer (0.24 mm).
_LEDGE_Z = 0.3


def _box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> list:
    c = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return [tuple(c[i] for i in reversed(f)) for f in faces]  # normals face out


def _write_stl(path: Path, triangles: list) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0, 0, 0))
            fh.writelines(struct.pack("<3f", *v) for v in tri)
            fh.write(struct.pack("<H", 0))
    return str(path)


@pytest.fixture
def ledge_part(tmp_path) -> str:
    """A 30 x 20 x 60 mm block on the plate with a 30 x 20 mm ledge beside
    it whose underside floats 0.3 mm above the plate."""
    return _write_stl(
        tmp_path / "ledge.stl",
        _box(0, 30, 0, 20, 0, 60) + _box(30, 60, 0, 20, _LEDGE_Z, 60),
    )


def test_the_estimator_prices_support_at_the_print_layer(ledge_part):
    estimator = CostEstimator()
    fine = estimator.estimate_from_mesh(ledge_part, include_supports=True, layer_height_mm=0.12)
    usual = estimator.estimate_from_mesh(ledge_part, include_supports=True, layer_height_mm=0.2)
    assert fine.support_weight_grams > 0, "at 0.12 mm the ledge is an overhang and needs support"
    assert usual.support_weight_grams == 0, "at 0.2 mm the ledge rests inside the plate band"


def test_the_report_prices_support_at_the_print_layer(tmp_path):
    """A part that needs support either way (a ledge 20 mm up) and has a
    second, low ledge 0.3 mm up: at a 0.12 mm layer the low ledge's underside
    is outside the plate band too, so the report's support plastic grows."""
    part = _write_stl(
        tmp_path / "two_ledges.stl",
        _box(0, 30, 0, 20, 0, 60)
        + _box(30, 60, 0, 20, 20, 60)
        + _box(-20, 0, 0, 20, _LEDGE_Z, 10),
    )
    fine, usual = (
        printability.analyze_printability(
            part, layer_height=layer, nozzle_diameter=0.4, material="PLA",
        )
        for layer in (0.12, 0.2)
    )
    assert fine.overhangs.needs_supports and usual.overhangs.needs_supports
    assert fine.cost is not None and usual.cost is not None
    assert fine.cost.assumptions["layer_height_mm"] == 0.12
    assert fine.cost.support_cost_usd > usual.cost.support_cost_usd


def test_a_saving_prices_the_same_print(ledge_part):
    """The lower-infill saving is the asked print minus the same print at
    15 %: the same nozzle and layer, not a 0.4 mm nozzle at 0.2 mm."""
    nozzle, layer = 0.6, 0.28
    cost = printability._analyze_cost(
        None, ledge_part, material="PETG", infill_percent=30.0,
        layer_height_mm=layer, nozzle_mm=nozzle,
    )
    estimator = CostEstimator()
    asked = estimator.estimate_from_mesh(
        ledge_part, material="PETG", infill_percent=30.0, wall_layers=3,
        layer_height_mm=layer, nozzle_mm=nozzle,
    )
    cheaper = estimator.estimate_from_mesh(
        ledge_part, material="PETG", infill_percent=15.0, wall_layers=3,
        layer_height_mm=layer, nozzle_mm=nozzle,
    )
    saving = asked.total_cost_usd - cheaper.total_cost_usd
    line = next(r for r in cost.cost_saving_recommendations if r.startswith("Reducing infill"))
    assert f"~${saving:.2f}" in line, line
