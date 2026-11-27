"""A brim never joins separate parts that print in place together.

A brim is laid around every part's outline on the first layer, so where two
separate parts stand closer on the bed than its two sides, it fills the gap
between them and joins them there: a print-in-place joint comes off the
plate welded.  The brim decision keeps that gap clear: it gives the widest
brim whose two sides leave a line clear between the parts, or none, and a
part that needs holding down is still told so, with what holds it down
instead.  A part that is one piece keeps its brim as before.

Each case runs through the registered tool, as a customer's install runs it.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402

GLUE = "ordinary glue stick on the clean plate"


def _save(meshes: list, tmp_path, name: str) -> str:
    path = tmp_path / name
    trimesh.util.concatenate(meshes).export(path)
    return str(path)


def _box(size: tuple[float, float, float], at: tuple[float, float, float]):
    """A box of *size* whose lowest corner is *at*."""
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation([a + s / 2.0 for a, s in zip(at, size, strict=True)])
    return mesh


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _walls_a_clearance_apart(tmp_path) -> str:
    """Two thin walls 40 mm tall standing 0.5 mm apart, printed in place
    together: each is too tall for its footing, and any brim fills the gap."""
    return _save(
        [_box((2.0, 30.0, 40.0), (0.0, 0.0, 0.0)), _box((2.0, 30.0, 40.0), (2.5, 0.0, 0.0))],
        tmp_path, "walls_apart.stl",
    )


def _walls_apart_on_the_bed(tmp_path) -> str:
    """The same two walls 8 mm apart on the bed, an arm from one reaching to
    0.3 mm of the other at the top: printed in place together, with room on
    the bed for a narrow brim and no more."""
    arm = _box((7.7, 30.0, 4.0), (2.3, 0.0, 36.0))
    return _save(
        [_box((2.0, 30.0, 40.0), (0.0, 0.0, 0.0)),
         trimesh.boolean.union([_box((2.0, 30.0, 40.0), (10.0, 0.0, 0.0)), arm], engine="manifold")],
        tmp_path, "walls_far.stl",
    )


def test_parts_a_clearance_apart_get_no_brim_and_are_told_what_holds_them(tmp_path):
    reply = _call("analyze_printability", file_path=_walls_a_clearance_apart(tmp_path))
    report = reply["report"]
    # Control: Kiln expects these walls to come off the bed, and reads them
    # as two parts printed in place together.
    assert report["adhesion_force"]["risk_level"] == "likely_detach", report["adhesion_force"]
    assert report["pieces"]["solid_count"] == 2, report["pieces"]
    adhesion = report["adhesion"]
    assert adhesion["brim_width_mm"] == 0 and not adhesion["use_raft"], adhesion
    assert adhesion["parts_gap_mm"] == pytest.approx(0.5, abs=0.02), adhesion
    assert reply["verdict"] == "check", reply["message"]
    assert (
        "likely to come off the bed before it finishes. Don't add a brim: it would join the separate "
        "pieces. Rub a thin layer of ordinary glue stick on the clean plate instead; it helps the first "
        "layer grip so the part doesn't come loose. If the part still lifts, turn on a raft in your "
        "slicer: a thin base printed underneath that peels off afterwards."
    ) in reply["message"], reply["message"]
    said = " ".join(text for _where, text in rx._sentences(reply))
    assert "mm brim" not in said, said
    assert adhesion["rationale"] in report["recommendations"], report["recommendations"]
    # The approval card's facts name the hold it is told, glue stick, and
    # never a brim.
    from kiln.printability import analyze_printability, check_reasons

    with rx.without_kiln_pro():
        facts = check_reasons(analyze_printability(_walls_a_clearance_apart(tmp_path)))
    assert {"id": "comes_off_bed", "hold": "glue"} in facts, facts
    assert not any(r.get("hold") == "brim" or "brim_mm" in r or r["id"] == "other" for r in facts), facts


def test_the_hold_said_is_the_same_for_every_plastic(tmp_path):
    """The same walls in ABS are told the same hold as in PLA: glue stick,
    then a raft if it still lifts."""
    path = _walls_a_clearance_apart(tmp_path)
    pla = _call("analyze_printability", file_path=path, material="PLA")
    abs_ = _call("analyze_printability", file_path=path, material="ABS")
    assert abs_["report"]["adhesion"]["brim_width_mm"] == 0 and not abs_["report"]["adhesion"]["use_raft"]
    for reply in (pla, abs_):
        assert GLUE in reply["message"] and "turn on a raft in your slicer:" in reply["message"], reply["message"]
    assert abs_["report"]["adhesion"]["rationale"] == pla["report"]["adhesion"]["rationale"]


def test_parts_further_apart_on_the_bed_get_the_widest_brim_that_leaves_the_gap(tmp_path):
    reply = _call("analyze_printability", file_path=_walls_apart_on_the_bed(tmp_path))
    report = reply["report"]
    assert report["adhesion_force"]["risk_level"] == "likely_detach", report["adhesion_force"]
    adhesion = report["adhesion"]
    # Two 3 mm brims and a 0.4 mm line fit in the 8 mm between them; 5 mm ones do not.
    assert adhesion["brim_width_mm"] == 3, adhesion
    assert adhesion["parts_gap_mm"] == pytest.approx(8.0, abs=0.02), adhesion
    assert (
        "before it finishes. Use a 3 mm brim, no wider: a wider one would join the separate pieces. "
        "Rub a thin layer of ordinary glue stick on the clean plate for extra hold."
    ) in reply["message"], reply["message"]
    # The approval card's facts carry both halves of that hold: the narrow
    # brim and the glue stick beside it.
    from kiln.printability import analyze_printability, check_reasons

    with rx.without_kiln_pro():
        facts = check_reasons(analyze_printability(_walls_apart_on_the_bed(tmp_path)))
    assert {"id": "comes_off_bed", "hold": "glue", "brim_mm": 3} in facts, facts


def test_a_tall_pillar_alone_keeps_its_brim(tmp_path):
    """One piece: no gap to keep, so the brim its height asks for stands."""
    reply = _call("analyze_printability", file_path=_save([_box((4.0, 4.0, 200.0), (0.0, 0.0, 0.0))], tmp_path, "p.stl"))
    adhesion = reply["report"]["adhesion"]
    assert adhesion["brim_width_mm"] == 8 and adhesion.get("parts_gap_mm") is None, adhesion
    assert reply["message"].startswith(
        "Check before printing: It is tall for what it stands on and is likely to come off the bed "
        "before it finishes, so print it with an 8 mm brim."
    ), reply["message"]
    assert GLUE not in reply["message"]


def test_separate_parts_not_printed_in_place_keep_their_brim(tmp_path):
    """Two tall pillars 3 mm apart and joined nowhere: a plate of separate
    parts, not parts printed in place together, keeps the brim it needs."""
    path = _save(
        [_box((4.0, 4.0, 200.0), (0.0, 0.0, 0.0)), _box((4.0, 4.0, 200.0), (7.0, 0.0, 0.0))], tmp_path, "two.stl",
    )
    adhesion = _call("analyze_printability", file_path=path)["report"]["adhesion"]
    assert adhesion["brim_width_mm"] == 8 and adhesion.get("parts_gap_mm") is None, adhesion


class TestTheDecisionItself:
    """:func:`kiln.printability.recommend_adhesion` with the gap between the
    parts on the bed: the widest brim that leaves it clear, or none, and the
    hold-down a raft gives is kept."""

    @staticmethod
    def _decide(pct: float, gap: float | None, material: str = "PLA"):
        from kiln.printability import BedAdhesionAnalysis, recommend_adhesion

        bed = BedAdhesionAnalysis(contact_area_mm2=pct * 10.0, contact_percentage=pct, adhesion_risk="high")
        return recommend_adhesion(bed, material=material, parts_gap_mm=gap, nozzle_mm=0.4)

    def test_no_gap_keeps_the_brim(self):
        rec = self._decide(1.0, None)
        assert (rec.brim_width_mm, rec.parts_gap_mm, rec.brim_left_off) == (8, None, False)

    def test_a_gap_wide_enough_changes_nothing(self):
        rec = self._decide(1.0, 16.4)
        assert (rec.brim_width_mm, rec.parts_gap_mm) == (8, None)

    @pytest.mark.parametrize(("gap", "width"), [(16.3, 5), (10.4, 5), (10.3, 3), (6.4, 3), (6.3, 0), (0.5, 0)])
    def test_the_widest_brim_that_leaves_the_gap_clear(self, gap, width):
        rec = self._decide(1.0, gap)
        assert rec.brim_width_mm == width and rec.parts_gap_mm == gap, rec
        assert rec.brim_left_off is (width == 0)
        assert rec.slicer_overrides == ({"brim_width": str(width), "brim_type": "outer_only"} if width else {})
        assert GLUE in rec.rationale, rec.rationale

    @pytest.mark.parametrize("material", ["ABS", "ASA", "NYLON", "PC"])
    @pytest.mark.parametrize(("gap", "width"), [(0.5, 0), (8.0, 3)])
    def test_the_hold_said_does_not_change_with_the_plastic(self, material, gap, width):
        """Where the brim is held back, every plastic is told the same hold."""
        rec = self._decide(20.0, gap, material=material)
        assert not rec.use_raft and rec.brim_width_mm == width and rec.parts_gap_mm == gap, rec
        assert rec.rationale == self._decide(20.0, gap, material="PLA").rationale, rec.rationale

    def test_a_raft_still_holds_it_down(self):
        rec = self._decide(1.0, 1.3, material="ABS")
        assert rec.use_raft and rec.brim_width_mm == 0 and not rec.brim_left_off, rec
        assert rec.rationale.endswith(
            "No brim with it: one would join its separate pieces where they stand 1.3 mm apart on the bed."
        ), rec.rationale
        assert rec.slicer_overrides == {"raft_layers": "3"}
