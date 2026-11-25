"""A gap drawn at the floor moving parts are given is never called close.

A rod drawn 0.3 mm inside its sleeve, both cut into 32 flat facets, measures
a hair under 0.3 mm across the middle of each facet.  A reply says that gap
as 0.3 mm, the smallest gap moving parts are given
(:func:`kiln.assembly.moving_gap_floor_mm`), so it never also says the two
may print stuck together.  A gap under the floor still is.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402

STUCK = "may print stuck together"


def _rod_in_sleeve(tmp_path, gap: float) -> str:
    rod = trimesh.creation.cylinder(radius=5.0, height=10.0, sections=32)
    sleeve = trimesh.creation.annulus(r_min=5.0 + gap, r_max=9.0, height=10.0, sections=32)
    mesh = trimesh.util.concatenate([rod, sleeve])
    mesh.apply_translation((0.0, 0.0, 5.0))
    path = tmp_path / f"rod_{gap}.stl"
    mesh.export(path)
    return str(path)


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def test_a_gap_drawn_at_the_floor_is_the_gap_moving_parts_are_given(tmp_path):
    from kiln.assembly import moving_gap_floor_mm

    assert moving_gap_floor_mm() == pytest.approx(0.3)
    reply = _call("analyze_printability", file_path=_rod_in_sleeve(tmp_path, 0.3))
    said = " ".join(text for _where, text in rx._sentences(reply))
    assert "stand 0.3 mm apart without touching" in said, said
    assert STUCK not in said, said
    assert reply["report"]["pieces"]["may_fuse"] == "", reply["report"]["pieces"]
    assert reply["verdict"] != "check", reply["message"]


def test_a_gap_under_the_floor_is_still_close(tmp_path):
    reply = _call("analyze_printability", file_path=_rod_in_sleeve(tmp_path, 0.25))
    assert reply["verdict"] == "check", reply["message"]
    assert STUCK in reply["message"], reply["message"]


def test_the_gap_is_judged_as_it_is_said():
    from kiln.printability import _may_print_stuck

    assert not _may_print_stuck(0.2986)  # said as 0.3 mm
    assert not _may_print_stuck(0.3)
    assert _may_print_stuck(0.294)  # said as 0.29 mm
    assert _may_print_stuck(0.05)
    assert not _may_print_stuck(None)
