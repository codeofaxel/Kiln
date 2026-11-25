"""A joint that may print stuck together is said, checked and shown in the approval.

Pieces drawn a hair apart read as one solid (they print as one), and the
gap between them is measured too: a lazy tongs with one joint drawn 0.05 mm
apart among nine at 0.3 mm says that pair, by its gap and where it is, and
no gap beside it is called fine.  Every pair that may print stuck together (a gap under the one moving
parts are given, :func:`kiln.assembly.moving_gap_floor_mm`) is a check: the
verdict asks for it, the print door's report is not ready, and the print's
approval carries the sentence and its reason, ``parts_may_fuse``.  A check
still prints once approved.  The good twins (every gap a real clearance)
stay ready with no such line.

A thin wall that meets thicker solid beside it is one wall: the reading
there is not also called surface detail the print loses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _print_in_place_parts as parts  # noqa: E402
from tests import _real_exports as rx  # noqa: E402
from tests.test_the_verdict_agrees_with_the_worst_finding import kilns_own_figures  # noqa: E402,F401

STUCK = "may print stuck together"
FINE = "fine for a designed clearance"
MOVING = "the way parts that move have to"


def _report(path: Path):
    from kiln.printability import analyze_printability

    with rx.without_kiln_pro():
        return analyze_printability(str(path))


def _tongs(tmp_path: Path, name: str = "tongs_tight.stl", **kwargs) -> Path:
    return parts.tongs(tmp_path / name, **kwargs)


def _tight_tongs(tmp_path: Path) -> Path:
    """Nine joints at 0.3 mm and the middle of the third pair at 0.05 mm."""
    return _tongs(tmp_path, gaps={parts.TONGS_MIDDLE_JOINT: 0.05})


def _from_left_mm(sentence: str) -> float:
    found = re.search(r"about ([0-9.]+) mm from its left side", sentence)
    assert found, sentence
    return float(found.group(1))


class TestOneFusedJointAmongGoodOnes:
    def test_the_pair_is_said_by_its_gap_and_where_it_is(self, tmp_path):
        from kiln.printability import check_reasons, checks_due, verdict

        report = _report(_tight_tongs(tmp_path))
        # Control: the reading prints the pair as one solid, so the eight
        # bars come out as seven parts.
        assert (report.pieces.piece_count, report.pieces.solid_count) == (8, 7)
        key, sentence = verdict(report)
        assert key == "check", sentence
        assert sentence.startswith(
            "Check before printing: Two of its pieces may print stuck together: they sit 0.05 mm apart, about "
        ), sentence
        # The middle joint of the third pair stands 132.7 mm from the part's
        # left side; its neighbours are a joint pitch (41 mm) away.
        assert 125.0 <= _from_left_mm(sentence) <= 137.0, sentence
        assert "Two of its pieces drawn apart sit 0.05 mm apart, closer than a printer keeps apart" in sentence
        assert FINE not in sentence and MOVING not in sentence, sentence
        assert "Elsewhere they stand 0.3 mm apart without touching, with nothing joining them." in sentence
        assert checks_due(report) == [report.pieces.may_fuse]
        assert check_reasons(report) == [{"id": "parts_may_fuse", "nearest_mm": 0.05, "pairs": 1}]
        assert report.printable is False

    def test_parts_said_as_moving_are_not_called_fine_beside_it(self, tmp_path):
        """With a motion note naming every bar a moving part, the gaps left
        are not said the way moving parts need while one joint is fused."""
        from kiln import mechanism_stage
        from kiln.printability import verdict

        def noted(path: Path) -> Path:
            spans = parts.runs(path)
            assert len(spans) == 8
            names = [f"bar_{i}" for i in range(len(spans))]
            block = {
                "kind": mechanism_stage.KIND, "playable": False, "moving_parts": names,
                "parts": {name: {"spans": [run]} for name, run in zip(names, spans, strict=True)},
            }
            assert mechanism_stage.record(str(path), block)
            return path

        # Control: the note is read, so the good twin's gaps are said as the
        # gaps moving parts need.
        assert MOVING in verdict(_report(noted(_tongs(tmp_path, "tongs_good.stl"))))[1]
        sentence = verdict(_report(noted(_tight_tongs(tmp_path))))[1]
        assert MOVING not in sentence and FINE not in sentence, sentence
        assert "Two of its pieces drawn apart sit 0.05 mm apart" in sentence
        assert "Elsewhere they stand 0.3 mm apart" in sentence

    def test_two_pieces_that_print_as_one_solid_are_said(self, tmp_path):
        """A disc standing in a ring a hair wider: the two print as one
        solid, which is said with the pair, never passed in silence."""
        from kiln.printability import verdict

        def rod_in_sleeve(name: str, gap: float) -> Path:
            rod = trimesh.creation.cylinder(radius=30.0, height=10.0, sections=64)
            sleeve = trimesh.creation.annulus(r_min=30.0 + gap, r_max=40.0, height=10.0, sections=64)
            mesh = trimesh.util.concatenate([rod, sleeve])
            mesh.apply_translation((0.0, 0.0, 5.0))
            mesh.export(tmp_path / name)
            return tmp_path / name

        report = _report(rod_in_sleeve("tight.stl", 0.05))
        assert (report.pieces.piece_count, report.pieces.solid_count) == (2, 1)
        key, sentence = verdict(report)
        assert key == "check", sentence
        assert sentence.startswith("Check before printing: Two of its pieces may print stuck together"), sentence
        assert "It prints as one solid, though it is drawn as 2 pieces: two of them sit 0.05 mm apart" in sentence

        clear = verdict(_report(rod_in_sleeve("clear.stl", 0.4)))[1]
        assert STUCK not in clear and FINE in clear, clear


    def test_the_way_to_make_them_one_solid_is_said_once(self, tmp_path):
        """A fused joint beside a tight one: each is said, and how to make
        parts one solid is said once."""
        from kiln.printability import verdict

        path = _tongs(tmp_path, gaps={parts.TONGS_MIDDLE_JOINT: 0.05, parts.TONGS_FIRST_CROSSING: 0.2})
        sentence = verdict(_report(path))[1]
        assert "Two of its pieces drawn apart sit 0.05 mm apart" in sentence, sentence
        assert "where the gap is 0.2 mm they may print stuck together" in sentence, sentence
        assert sentence.count("sink them at least 0.4 mm into each other") == 1, sentence

    @pytest.mark.parametrize(("posts", "unchecked"), [(26, 2), (20, 0)])
    def test_pairs_past_what_is_measured_are_said_not_checked(self, tmp_path, posts, unchecked):
        """Posts sunk into a plate print as one solid with it.  Past the
        pairs one reading measures, the rest are said as not checked, so the
        answer never reads as if every joint had been measured."""
        from kiln.printability import verdict

        plate = trimesh.creation.box(extents=(140.0, 60.0, 4.0))
        plate.apply_translation((70.0, 30.0, 2.0))
        sunk = []
        for i in range(posts):
            post = trimesh.creation.box(extents=(4.0, 4.0, 10.0))
            post.apply_translation((6.0 + 10.0 * (i % 13), 15.0 + 30.0 * (i // 13), 8.0))  # 1 mm into the plate
            sunk.append(post)
        path = tmp_path / f"posts_{posts}.stl"
        trimesh.util.concatenate([plate, *sunk]).export(path)
        report = _report(path)
        # Control: every post is one solid with the plate.
        assert (report.pieces.piece_count, report.pieces.solid_count) == (posts + 1, 1)
        sentence = verdict(report)[1]
        if unchecked:
            assert (
                f"Not checked: {unchecked} pairs of its pieces that print as one solid, past what this check "
                "measures."
            ) in sentence, sentence
        else:
            assert "Not checked" not in sentence, sentence
        assert STUCK not in sentence


class TestAGapAtTheFloorIsAtTheFloor:
    """A gap is judged the way it is said.  Two parts drawn the floor apart
    read a hair under it off their facets (a 0.3 mm gap drawn round with 32
    facets a turn measures 0.299 mm) and are said "0.3 mm apart": that is
    the floor itself, so the same sentence never also says they may print
    stuck together.  A gap said as less than the floor is under it."""

    @staticmethod
    def _two_bars(tmp_path: Path, name: str, gap: float) -> Path:
        near = trimesh.creation.box(extents=(20.0, 10.0, 10.0))
        near.apply_translation((10.0, 0.0, 5.0))
        far = trimesh.creation.box(extents=(20.0, 10.0, 10.0))
        far.apply_translation((30.0 + gap, 0.0, 5.0))
        trimesh.util.concatenate([near, far]).export(tmp_path / name)
        return tmp_path / name

    def test_a_gap_said_as_the_floor_is_not_called_stuck(self, tmp_path):
        from kiln.printability import verdict

        report = _report(self._two_bars(tmp_path, "at_floor.stl", 0.2994))
        sentence = verdict(report)[1]
        assert "0.3 mm apart" in sentence, sentence
        assert STUCK not in sentence, sentence
        assert not report.pieces.may_fuse, report.pieces.may_fuse

    def test_a_gap_said_under_the_floor_still_is(self, tmp_path):
        from kiln.printability import verdict

        sentence = verdict(_report(self._two_bars(tmp_path, "under.stl", 0.29)))[1]
        assert STUCK in sentence and "0.29 mm" in sentence, sentence


class TestTouchingOrSunkPiecesAreOneSolid:
    """Pieces that touch or sink into one another are one solid as drawn:
    nothing is said of them, and a hollow part's inner wall is never a pair."""

    def test_a_letter_sunk_into_a_sign(self, tmp_path):
        from kiln.printability import verdict

        sign = trimesh.creation.box(extents=(80.0, 30.0, 4.0))
        sign.apply_translation((40.0, 15.0, 2.0))
        letter = trimesh.creation.box(extents=(10.0, 14.0, 3.0))
        letter.apply_translation((20.0, 15.0, 5.0))  # 0.5 mm into the sign
        path = tmp_path / "sign.stl"
        trimesh.util.concatenate([sign, letter]).export(path)
        report = _report(path)
        assert report.pieces.solid_count == 1
        assert STUCK not in verdict(report)[1]

    def test_a_hollow_box(self, tmp_path):
        from kiln.printability import verdict

        # Walls 0.2 mm thick, under the moving-parts gap: the inner wall is
        # the part's own material, never a second piece that far apart.
        outer = trimesh.creation.box(extents=(40.0, 40.0, 30.0))
        inner = trimesh.creation.box(extents=(39.6, 39.6, 29.6))
        inner.invert()
        hollow = trimesh.util.concatenate([outer, inner])
        hollow.apply_translation((0.0, 0.0, 15.0))
        path = tmp_path / "hollow.stl"
        hollow.export(path)
        report = _report(path)
        assert report.pieces.piece_count == 2
        assert STUCK not in verdict(report)[1]


class TestThePrintDoorAndTheApproval:
    """The print door's own gate, and the approval built from it."""

    @staticmethod
    def _approval(path: Path):
        from kiln import print_consent
        from kiln.plugins.validation_pipeline_tools import check_due, gate_for_print

        with rx.without_kiln_pro():
            gate = gate_for_print(str(path))
            states = print_consent.what_the_print_states(path.name, str(path))
        return gate, check_due(gate.report), states, print_consent.print_rows_of(states)

    @pytest.mark.parametrize(
        ("name", "build", "nearest_mm"),
        [
            ("tongs_tight.stl", lambda p: parts.tongs(p, gaps={parts.TONGS_MIDDLE_JOINT: 0.05}), 0.05),
            ("claw_tight.stl", lambda p: parts.claw(p, gap=0.05, rod_gap=0.05), 0.04),
            ("claw_g021.stl", lambda p: parts.claw(p, gap=0.21, rod_gap=0.21), 0.18),
        ],
    )
    def test_a_part_that_may_print_stuck_is_a_check_and_the_approval_says_so(
        self, tmp_path, name, build, nearest_mm,
    ):
        path = build(tmp_path / name)
        gate, said, states, rows = self._approval(path)
        # Warned, never held: the print door lets it through to the approval.
        assert gate.reason is None, gate.reason
        assert gate.report["ready_to_print"] is False
        assert gate.report["printability_verdict"] == "check"
        assert said is not None and STUCK in said.split(". ")[0], said
        assert rows["check"] == states["check"] == said
        reasons = states["part_check"]["reasons"]
        fused = [r for r in reasons if r["id"] == "parts_may_fuse"]
        assert fused and fused[0]["nearest_mm"] == nearest_mm, reasons
        # The card counts the pairs the check names, never a fixed "two".
        assert isinstance(fused[0]["pairs"], int) and fused[0]["pairs"] >= 1, reasons

    @pytest.mark.parametrize(
        ("name", "build"),
        [
            ("tongs_good.stl", lambda p: parts.tongs(p)),
            ("claw_good35.stl", lambda p: parts.claw(p, gap=0.35, rod_gap=0.35)),
        ],
    )
    def test_the_good_twins_stay_ready_with_no_row(self, tmp_path, name, build):
        path = build(tmp_path / name)
        gate, said, states, rows = self._approval(path)
        assert gate.reason is None
        assert gate.report["ready_to_print"] is True
        assert gate.report["printability_verdict"] == "prints_with_supports"
        assert said is None and rows == {} and "part_check" not in states
        assert STUCK not in gate.report["printability_verdict_text"]
        assert FINE in gate.report["printability_verdict_text"]

    def test_the_account_card_carries_the_reason(self):
        from kiln import bridge_client

        fact = {"verdict": "check", "reasons": [{"id": "parts_may_fuse", "nearest_mm": 0.05}]}
        assert bridge_client._part_check_fact(fact) == fact
        past_the_floor = {"verdict": "check", "reasons": [{"id": "parts_may_fuse", "nearest_mm": 0.5}]}
        assert bridge_client._part_check_fact(past_the_floor) is None


class TestAThinHandleIsAWall:
    """The tongs' finger ring drawn 0.2 mm thick: where it meets the bar, a
    reading looks like a fin on the bar's solid, and the ring is still one
    wall, never also surface detail the print loses."""

    def test_on_the_general_figures(self, tmp_path):
        report = _report(_tongs(tmp_path, "tongs_thin.stl", ring_wall=0.2))
        walls = report.thin_walls
        assert walls.fine_detail_count == 0, walls
        assert not any(r.startswith("Surface detail") for r in report.recommendations), report.recommendations
        assert walls.may_be_left_out_count > 0 and walls.may_be_left_out_min_mm == pytest.approx(0.2, abs=0.01)

    def test_on_kilns_own_figures(self, tmp_path, kilns_own_figures):
        from kiln.printability import verdict

        report = _report(_tongs(tmp_path, "tongs_thin.stl", ring_wall=0.2))
        walls = report.thin_walls
        assert walls.slicer_check == "exact", walls
        assert walls.fine_detail_count == 0, walls
        assert not any(r.startswith("Surface detail") for r in report.recommendations), report.recommendations
        assert verdict(report)[1].startswith("Check before printing: Walls as thin as 0.20 mm"), verdict(report)[1]
