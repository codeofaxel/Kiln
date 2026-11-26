"""A face standing on edge at the bottom of a part is not bed contact.

A CAD export can leave thin vertical faces inside the first layer: the foot
of a wall, the end of a rounded hinge knuckle, the seam where a pin meets a
plate.  They cover no bed.  Read as contact they add outline edges that bound
nothing, and the footing reading can then call a flat plate likely to come
off the bed.

The rule: the contact every footing reader shares (``_bed_contact``) keeps
only faces that cover some of the bed, so a flat plate stands on its plate
whatever edge-on faces its file carries.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.printability import _bed_contact, _footing_section_modulus, analyze_printability  # noqa: E402

# An edge-on face along one side of the plate, one micron tall: the shape a
# hinge pin's end leaves where it meets the plate's side.
_SLIVER = np.array([[[80.0, -64.3, 0.0], [80.0, -4.3, 0.0], [80.0, -4.3, 0.001]]])


def _plate_with_lip() -> np.ndarray:
    """An 80 x 60 x 4 mm plate with a 3 mm lip standing 12 mm tall: a phone
    stand's base, nothing tall or narrow about it."""
    plate = trimesh.creation.box(extents=[80, 60, 4]).apply_translation([40, -34.3, 2])
    lip = trimesh.creation.box(extents=[80, 3, 8]).apply_translation([40, -56.8, 8])
    return np.asarray(trimesh.util.concatenate([plate, lip]).triangles, dtype=np.float64)


def _as_list(tris: np.ndarray) -> list:
    return [tuple(map(tuple, t)) for t in tris]


def test_an_edge_on_face_is_left_out_of_the_contact():
    clean = _plate_with_lip()
    with_sliver = np.concatenate([clean, _SLIVER])
    a = _bed_contact(_as_list(clean), 0.0)
    b = _bed_contact(_as_list(with_sliver), 0.0)
    assert len(b.tris) == len(a.tris)
    assert b.area == pytest.approx(a.area)


def test_the_footing_reads_the_same_with_an_edge_on_face():
    clean = _plate_with_lip()
    with_sliver = np.concatenate([clean, _SLIVER])
    whole = _footing_section_modulus(_as_list(clean), 0.0, height=12.0, tris=clean)
    assert whole > 1000.0
    assert _footing_section_modulus(
        _as_list(with_sliver), 0.0, height=12.0, tris=with_sliver,
    ) == pytest.approx(whole)


def test_a_flat_plate_is_not_said_to_come_off_the_bed(tmp_path: Path):
    tris = np.concatenate([_plate_with_lip(), _SLIVER])
    path = tmp_path / "stand_base.stl"
    trimesh.Trimesh(
        vertices=tris.reshape(-1, 3), faces=np.arange(len(tris) * 3).reshape(-1, 3), process=False,
    ).export(path)
    report = analyze_printability(str(path), material="PLA")
    assert report.adhesion_force.risk_level == "secure"


def test_a_part_whose_only_bottom_faces_stand_on_edge_has_no_contact():
    contact = _bed_contact(_as_list(_SLIVER), 0.0)
    assert len(contact.tris) == 0
    assert contact.area == 0.0
