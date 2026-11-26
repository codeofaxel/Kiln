"""A pin a designed clearance inside its bore is two solids at any size.

The pieces reading (:mod:`kiln.mesh_bodies`) asks whether one piece lies
against or sinks into another by looking just outside its surface.  A
print-in-place clearance (0.3 mm here, a hinge pin in its knuckle) has to stay
a gap on a part a large printer makes as well as on a small one: a pin 330 mm
long standing 0.3 mm inside its bore prints as two parts, the same as one
80 mm long.

The guards keep the true joins: a pin thicker than its bore, a pin that fills
its bore exactly, and a pin closer to its bore than a printer keeps apart all
print as one part with it.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.fusion_check import judge_reading, printed_parts  # noqa: E402
from kiln.mesh_bodies import read_mesh  # noqa: E402

_BORE_MM = 2.3


def _pin_in_bore(length: float, clearance: float) -> trimesh.Trimesh:
    """A pin lying along x inside a knuckle bored to :data:`_BORE_MM`, both
    *length* mm long, the pin *clearance* mm smaller in radius than the bore
    (a negative clearance makes the pin thicker than the bore)."""
    knuckle = trimesh.creation.annulus(r_min=_BORE_MM, r_max=4.0, height=length, sections=64)
    pin = trimesh.creation.cylinder(radius=_BORE_MM - clearance, height=length, sections=64)
    pair = trimesh.util.concatenate([knuckle, pin])
    pair.apply_transform(trimesh.transformations.rotation_matrix(1.5707963267948966, [0, 1, 0]))
    return pair


def _printed(mesh: trimesh.Trimesh) -> int:
    """How many parts *mesh* prints as: its solids, joined where they stand
    closer than a printer keeps apart."""
    reading = read_mesh(mesh)
    stood = judge_reading(mesh.triangles, reading, clearances=True)
    touching = list((stood.get("clearances") or {}).get("touching") or [])
    return len(set(printed_parts(reading, {"touching": touching})))


@pytest.mark.parametrize("length", [80.0, 330.0, 600.0])
def test_a_designed_clearance_reads_as_two_solids_at_any_length(length: float):
    mesh = _pin_in_bore(length, 0.3)
    reading = read_mesh(mesh)
    assert reading.piece_count == 2
    assert reading.solid_count == 2
    assert _printed(mesh) == 2


@pytest.mark.parametrize("length", [80.0, 330.0, 600.0])
def test_a_pin_thicker_than_its_bore_is_one_solid_with_it(length: float):
    assert read_mesh(_pin_in_bore(length, -0.3)).solid_count == 1


@pytest.mark.parametrize("length", [80.0, 330.0])
def test_a_pin_that_fills_its_bore_is_one_solid_with_it(length: float):
    assert read_mesh(_pin_in_bore(length, 0.0)).solid_count == 1


@pytest.mark.parametrize("length", [80.0, 330.0, 600.0])
def test_a_pin_closer_than_a_printer_keeps_apart_prints_as_one_part(length: float):
    assert read_mesh(_pin_in_bore(length, 0.02)).solid_count == 1
    assert _printed(_pin_in_bore(length, 0.02)) == 1
