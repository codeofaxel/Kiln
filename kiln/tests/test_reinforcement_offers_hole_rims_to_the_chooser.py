"""Reinforcing a part hands every hole rim to the edge chooser, and says what
happened to the holes when a fastener was named.

The reinforcement step rounds a part's sharp inside edges and nothing else
(``test_reinforcement_rounds_inside_corners``).  A hole's rim is an outside
edge, so until 2026-10-24 that selection never reached the chooser kiln-pro
may hand in, and the ``fastener`` a person named did nothing there: no rim
was ever offered, whatever the chooser knew about the screw.  Now every rim
is offered under any selection; one the chooser says nothing about stays as
the selection had it, and a free caller who named a fastener is told once
that the holes were left as drawn.

The parts are CAD files built on the kernel the edge step rounds on, and
reinforced through the engine's one door, with a chooser standing in for
kiln-pro's where one is needed.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402
from tests.test_edge_finish import _box, _cut, _cyl, _fuse, _write_step, needs_cad_kernel  # noqa: E402

pytestmark = needs_cad_kernel

HOLE_MM = 2.6


def _bracket_with_hole(tmp_path):
    """An L-bracket (a 40 x 30 x 4 base, a 4 mm upright 30 tall) with a 2.6 mm
    hole through its base: one inside corner, two hole rims."""
    shape = _cut(_fuse(_box(0, 0, 0, 40, 30, 4), _box(0, 0, 0, 4, 30, 30)), _cyl(25, 15, -1, HOLE_MM / 2, 6))
    return _write_step(shape, tmp_path / "bracket.step")


def _reinforce(step, tmp_path, monkeypatch, chooser, fastener="M3"):
    import kiln.design_reasoning as dr

    monkeypatch.setattr(dr, "_apply_base", lambda *_a, **_k: None)
    monkeypatch.setattr(dr, "_edge_policy", lambda material, fastener=None: chooser)
    return dr.apply_reinforcements(
        step, output_path=str(tmp_path / "reinforced.stl"), fillet_radius_mm=1.0,
        nozzle_mm=0.4, layer_height_mm=0.2, fastener=fastener,
    )


def _rounding(result):
    rounded = [a for a in result.applied if a["type"] == "fillet"]
    assert len(rounded) == 1, result.skipped
    return rounded[0]["edges"]


def test_every_hole_rim_is_offered_to_the_chooser_while_only_inside_edges_are_rounded(tmp_path, monkeypatch):
    seen: dict[int, dict] = {}

    def chooser(chains, frame):
        seen.update({c["chain"]: c for c in chains})
        return {}

    result = _reinforce(_bracket_with_hole(tmp_path), tmp_path, monkeypatch, chooser)
    rims = [c for c in seen.values() if c.get("held") == "hole_rim"]
    assert len(rims) == 2 and {c["hole_mm"] for c in rims} == {HOLE_MM}, seen
    assert all(c["corner"] == "outside" for c in rims)
    assert "unasked" not in rims[0]  # the chooser is handed facts, not the plan's bookkeeping
    edges = _rounding(result)
    # The chooser said nothing, so the rims stay as the selection had them: not
    # selected, not "left sharp", and the inside corner was rounded as asked.
    assert [f["corner"] for f in edges["finished"]] == ["inside"]
    assert not [s for s in edges["left_sharp"] if "rims a" in s["reason"]], edges["left_sharp"]
    assert edges["not_selected"] >= 2


def test_a_rim_the_chooser_finishes_is_finished_and_the_hole_is_kept(tmp_path, monkeypatch):
    def chooser(chains, frame):
        return {
            c["chain"]: ("chamfer", 0.4, f"lead-in for the screw named on a {c['hole_mm']:g} mm hole")
            for c in chains if c.get("held") == "hole_rim" and c["place"] == "top"
        }

    result = _reinforce(_bracket_with_hole(tmp_path), tmp_path, monkeypatch, chooser)
    edges = _rounding(result)
    bevels = [f for f in edges["finished"] if f["finish"] == "chamfer"]
    assert len(bevels) == 1 and bevels[0]["corner"] == "outside" and bevels[0]["size_mm"] == pytest.approx(0.4)
    assert "lead-in for the screw named" in bevels[0]["note"]
    assert [f["corner"] for f in edges["finished"] if f["finish"] == "fillet"] == ["inside"]
    assert result.print_aware == (
        "Each edge's finish was chosen for how it prints on this printer, and each hole for the M3 you named."
    )
    after = trimesh.load(result.output_path, force="mesh")
    assert after.is_watertight
    # The hole is still a 2.6 mm hole on the CAD: the bevel widened its mouth only.
    from kiln.cad_edge import survey_step

    assert survey_step(result.step_path)["holes_mm"] == [HOLE_MM]


def test_a_rim_the_chooser_leaves_alone_is_listed_with_its_reason(tmp_path, monkeypatch):
    def chooser(chains, frame):
        return {c["chain"]: (None, 0.0, "left as drawn: the screw named passes through this hole")
                for c in chains if c.get("held") == "hole_rim"}

    edges = _rounding(_reinforce(_bracket_with_hole(tmp_path), tmp_path, monkeypatch, chooser))
    said = [s for s in edges["left_sharp"] if "passes through this hole" in s["reason"]]
    assert len(said) == 2 and all(s["corner"] == "outside" for s in said)


def test_a_free_caller_who_named_a_fastener_is_told_the_holes_were_left_as_drawn(tmp_path, monkeypatch):
    """Through the tool a person calls, as a plain install answers it: the
    holes are left as drawn, and the tier that prepares them is named once."""
    import kiln.design_reasoning as dr
    from kiln import server

    monkeypatch.setattr(dr, "_apply_base", lambda *_a, **_k: None)
    step = _bracket_with_hole(tmp_path)
    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, "apply_design_reinforcements", {
            "file_path": step, "output_path": str(tmp_path / "reinforced.stl"), "fillet_radius_mm": 1.0,
            "nozzle_mm": 0.4, "layer_height_mm": 0.2, "fastener": "M3",
        })
    assert reply.get("success"), reply
    said = reply["print_aware"]
    assert "The holes are left as drawn for the M3 you named." in said
    assert said.count("Kiln Pro") == 1 and "kiln3d.com/pricing" in said and "prepares each hole" in said
    # And with no fastener named there is nothing to say about holes.
    with rx.without_kiln_pro():
        plain = rx.call(server.mcp, "apply_design_reinforcements", {
            "file_path": step, "output_path": str(tmp_path / "plain.stl"), "fillet_radius_mm": 1.0,
            "nozzle_mm": 0.4, "layer_height_mm": 0.2,
        })
    assert "holes" not in plain.get("print_aware", "")
