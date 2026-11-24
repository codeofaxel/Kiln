"""The thermal-stress check says which reason raised its risk, and its advice
follows that reason.

``raised_by`` is ``"geometry"`` when the part has a change in cross-section
that reaches the risk on its own, ``"material"`` when its section holds
steady and the plastic alone raised it, and ``""`` at low risk.  Advice that
speaks of a transition or a geometry change is given only for
``"geometry"``, so a column of one section is never told to chamfer a
change it does not have.

A plastic's stress factor is stood in for with a number no real setting has.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")

STRESS_FACTOR = 2.37  # stand-in for a warp-prone plastic's factor (the floor: 1.0)

#: Words only advice about a change in section uses.
_GEOMETRY_WORDS = ("transition", "geometry change", "chamfer", "variation", "abruptly")


def _save(mesh, tmp_path, name: str) -> str:
    path = tmp_path / name
    mesh.export(path)
    return str(path)


@pytest.fixture()
def column(tmp_path) -> str:
    """A 20 x 20 mm column 60 mm tall: one section from bed to top."""
    part = trimesh.creation.box(extents=(20.0, 20.0, 60.0))
    part.apply_translation((0.0, 0.0, 30.0))
    return _save(part, tmp_path, "column.stl")


@pytest.fixture()
def stepped(tmp_path) -> str:
    """A 40 mm block with a 2 mm fin standing on it: an abrupt step."""
    base = trimesh.creation.box(extents=(40.0, 40.0, 10.0))
    base.apply_translation((0.0, 0.0, 5.0))
    fin = trimesh.creation.box(extents=(2.0, 20.0, 30.0))
    fin.apply_translation((0.0, 0.0, 25.0))
    return _save(trimesh.util.concatenate([base, fin]), tmp_path, "stepped.stl")


@pytest.fixture()
def warp_prone(monkeypatch) -> None:
    import kiln.printability as P

    monkeypatch.setattr(P, "_material_stress_factor", lambda _material: STRESS_FACTOR)


def _thermal(path: str, material: str):
    from kiln.printability import analyze_printability
    from tests import _real_exports as rx

    with rx.without_kiln_pro():
        return analyze_printability(path, material=material).thermal_stress


def _speaks_of_geometry(lines: list[str]) -> list[str]:
    return [line for line in lines if any(word in line.lower() for word in _GEOMETRY_WORDS)]


@pytest.mark.usefixtures("warp_prone")
def test_a_column_of_one_section_in_a_warp_prone_plastic_is_raised_by_the_material(column):
    thermal = _thermal(column, "abs")
    assert thermal.risk_level != "low", "control: the plastic alone raises the risk"
    assert thermal.max_area_change_ratio == pytest.approx(1.0, abs=0.05)
    assert thermal.raised_by == "material"
    assert thermal.to_dict()["raised_by"] == "material"
    assert _speaks_of_geometry(thermal.recommendations) == [], thermal.recommendations
    assert any("abs" in line.lower() for line in thermal.recommendations), thermal.recommendations


def test_a_stepped_part_in_pla_is_raised_by_its_geometry(stepped):
    thermal = _thermal(stepped, "pla")
    assert thermal.risk_level != "low", "control: the step raises the risk on the floor"
    assert thermal.raised_by == "geometry"
    assert _speaks_of_geometry(thermal.recommendations), thermal.recommendations


def test_a_part_at_low_risk_names_no_reason(column):
    thermal = _thermal(column, "pla")
    assert (thermal.risk_level, thermal.raised_by, thermal.recommendations) == ("low", "", [])
