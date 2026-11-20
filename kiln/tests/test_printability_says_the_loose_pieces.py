"""The printability verdict says when a part prints as more than one piece.

A bookmark whose text is cut straight through comes out as several solids,
the body and the middles of the closed letters, and a check that never looks
at the pieces says "Ready to print."  Every door that gives the verdict
says it: separate parts on the plate print, and each comes off on its
own; separate parts that stand a clearance apart print in place together and
are never told to be joined; a piece resting on the part, or a clearance away
from it, prints loose on it; a piece in the air that nothing joins or holds
means the part will not print as it is, in the fusion check's words.  Pieces
that share a surface or sink into one another are one solid and are not
mentioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the parts

from kiln.printability import analyze_printability, verdict  # noqa: E402

SEPARATE = "separate parts on the plate"


@pytest.fixture(autouse=True)
def public_floor(monkeypatch):
    """Public Kiln's own floor answers: every ``kiln_pro`` import fails."""
    import sys

    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _box(lo, hi):
    mesh = trimesh.creation.box(extents=np.subtract(hi, lo))
    mesh.apply_translation(np.add(lo, hi) / 2.0)
    return mesh


def _ring_cut_through(path: Path) -> Path:
    """A 40 x 24 x 1.2 mm plate with an O cut through it: the middle of the
    O is left standing on the plate, joined to nothing."""
    plate = _box((-20, -12, 0), (20, 12, 1.2))
    outer = trimesh.creation.cylinder(radius=5.0, height=4.0, sections=64)
    inner = trimesh.creation.cylinder(radius=3.5, height=5.0, sections=64)
    part = plate.difference(outer.difference(inner))
    part.export(path)
    return path


def _with_a_piece_in_the_air(path: Path) -> Path:
    """A block with a coat laid on it, and a cube 15 mm up joined to nothing."""
    parts = [_box((0, 0, 0), (30, 30, 10)), _box((0, 0, 10), (30, 30, 12)), _box((40, 0, 15), (44, 4, 19))]
    trimesh.util.concatenate(parts).export(path)
    return path


def _lid_a_clearance_above(path: Path) -> Path:
    """A block with a smaller one sitting 0.2 mm above it, as a print-in-place lid."""
    trimesh.util.concatenate([_box((0, 0, 0), (30, 30, 10)), _box((5, 5, 10.2), (15, 15, 14))]).export(path)
    return path


def _slider_in_its_slot(path: Path, *, with_a_block_beside: bool = False) -> Path:
    """A block with a T-shaped slot closed at both ends, and a T-shaped slider
    printed in place in it 0.4 mm clear all round: both stand on the plate,
    nothing joins them, and the slider slides but cannot come out."""
    block = _box((-20, -12, 0), (20, 12, 10))
    block = block.difference(_box((-15, -7, -1), (15, 7, 6))).difference(_box((-15, -3, 5.9), (15, 3, 11)))
    slider = _box((-10, -6.6, 0), (10, 6.6, 5.6)).union(_box((-10, -2.6, 5.5), (10, 2.6, 12)))
    parts = [block, slider]
    if with_a_block_beside:
        parts.append(_box((30, -5, 0), (40, 5, 10)))
    trimesh.util.concatenate(parts).export(path)
    return path


def _two_blocks_apart(path: Path) -> Path:
    """Two blocks on the plate 10 mm apart: genuinely separate parts."""
    trimesh.util.concatenate([_box((0, 0, 0), (20, 20, 10)), _box((30, 0, 0), (40, 10, 10))]).export(path)
    return path


def _coated(path: Path) -> Path:
    """A block, a coat on its top face and three sprinkles sunk into the coat:
    five pieces, one solid."""
    parts = [_box((0, 0, 0), (30, 30, 10)), _box((0, 0, 10), (30, 30, 12))]
    for x in (8, 15, 22):
        sprinkle = trimesh.creation.cylinder(radius=1.0, height=8.0, sections=48)
        sprinkle.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
        sprinkle.apply_translation((x, 15, 12.5))
        parts.append(sprinkle)
    trimesh.util.concatenate(parts).export(path)
    return path


class TestTheVerdictSaysThePieces:
    def test_a_middle_cut_loose_is_a_separate_part(self, tmp_path):
        report = analyze_printability(str(_ring_cut_through(tmp_path / "o.stl")))
        key, sentence = verdict(report)
        assert (report.pieces.piece_count, report.pieces.solid_count) == (2, 2)
        assert report.pieces.floating_bodies == 0
        assert key == "ready"
        assert sentence.startswith("Ready to print. ")
        assert f"It prints as 2 {SEPARATE}" in sentence
        assert "comes off the plate on its own" in sentence

    def test_a_piece_in_the_air_means_it_will_not_print(self, tmp_path):
        report = analyze_printability(str(_with_a_piece_in_the_air(tmp_path / "air.stl")))
        key, sentence = verdict(report)
        assert report.pieces.floating_bodies == 1
        assert key == "wont_print"
        assert sentence.startswith("Won't print as it is: 1 piece floats with nothing under it")
        assert report.printable is False
        assert report.recommendations[0] == report.pieces.floating

    def test_a_piece_a_clearance_above_the_part_prints_loose_on_it(self, tmp_path):
        report = analyze_printability(str(_lid_a_clearance_above(tmp_path / "lid.stl")))
        key, sentence = verdict(report)
        assert key != "wont_print"
        assert "1 piece sits less than 0.8 mm from the part" in sentence
        assert "fine for a designed clearance. If it was meant to be one solid with the part" in sentence
        assert "belongs to the part" not in sentence
        assert SEPARATE not in sentence

    def test_parts_printed_in_place_a_clearance_apart_are_never_told_to_join(self, tmp_path):
        report = analyze_printability(str(_slider_in_its_slot(tmp_path / "slider.stl")))
        key, sentence = verdict(report)
        assert (report.pieces.solid_count, report.pieces.floating_bodies) == (2, 0)
        assert key != "wont_print"
        assert f"It prints as 2 {SEPARATE}, not one" in sentence
        assert (
            "They stand 0.4 mm apart without touching, so they print in place together with nothing "
            "joining them: fine for a designed clearance."
        ) in sentence
        assert "join them so they overlap" not in sentence
        assert "comes off the plate on its own" not in sentence

    def test_genuinely_loose_parts_are_still_told_to_join(self, tmp_path):
        sentence = verdict(analyze_printability(str(_two_blocks_apart(tmp_path / "two.stl"))))[1]
        assert (
            f"It prints as 2 {SEPARATE}, not one: the largest is 20 x 20 x 10 mm and one 10 x 10 x 10 mm, "
            "and each comes off the plate on its own. If they belong together, join them so they overlap."
        ) in sentence
        assert "print in place" not in sentence

    def test_a_loose_part_beside_parts_printed_in_place(self, tmp_path):
        path = _slider_in_its_slot(tmp_path / "s.stl", with_a_block_beside=True)
        sentence = verdict(analyze_printability(str(path)))[1]
        assert f"It prints as 3 {SEPARATE}, not one" in sentence
        assert "2 of them stand 0.4 mm apart without touching, so they print in place together" in sentence
        assert sentence.endswith(
            "The other one comes off the plate on its own; if it belongs with them, join it so they overlap."
        )

    def test_pieces_that_print_as_one_solid_say_nothing(self, tmp_path):
        report = analyze_printability(str(_coated(tmp_path / "coated.stl")))
        assert (report.pieces.piece_count, report.pieces.solid_count) == (5, 1)
        assert report.pieces.note == "" and report.pieces.floating == ""
        assert "separate parts" not in verdict(report)[1]

    def test_the_reply_carries_the_pieces(self, tmp_path):
        served = analyze_printability(str(_ring_cut_through(tmp_path / "o.stl"))).to_dict()["pieces"]
        assert (served["piece_count"], served["solid_count"], served["floating_bodies"]) == (2, 2, 0)
        assert [round(v) for v in served["parts_mm"][0]] == [40, 24, 1]


# ---------------------------------------------------------------------------
# Every door that gives the verdict says it
# ---------------------------------------------------------------------------


def _register(plugin_cls) -> dict[str, Any]:
    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    plugin_cls().register(_FakeMcp())
    return tools


@pytest.fixture
def no_auth(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)


@pytest.fixture
def loose(tmp_path) -> str:
    return str(_ring_cut_through(tmp_path / "o.stl"))


@pytest.fixture
def floating(tmp_path) -> str:
    return str(_with_a_piece_in_the_air(tmp_path / "air.stl"))


class TestEveryDoorSaysIt:
    def test_the_printability_tool(self, loose, floating, no_auth):
        from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

        analyze = _register(_PrintabilityToolsPlugin)["analyze_printability"]
        assert SEPARATE in analyze(file_path=loose)["message"]
        assert analyze(file_path=floating)["message"].startswith("Won't print as it is: 1 piece floats")

    def test_the_printability_tool_on_parts_printed_in_place(self, tmp_path, no_auth):
        from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

        analyze = _register(_PrintabilityToolsPlugin)["analyze_printability"]
        message = analyze(file_path=str(_slider_in_its_slot(tmp_path / "slider.stl")))["message"]
        assert "print in place together with nothing joining them" in message
        assert "join them so they overlap" not in message

    def test_the_pre_print_check(self, loose, floating):
        from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

        assert SEPARATE in run_full_validation_pipeline(loose)["summary"]
        refused = run_full_validation_pipeline(floating)
        assert refused["printability_verdict"] == "wont_print"
        assert "1 piece floats" in refused["summary"]

    def test_the_mesh_validation_pipeline(self, loose, floating):
        from kiln.mesh_validation_pipeline import run_validation_pipeline

        assert SEPARATE in run_validation_pipeline(loose).summary
        refused = run_validation_pipeline(floating)
        assert refused.passed is False and "1 piece floats" in refused.summary

    def test_the_readiness_check(self, loose, floating):
        from kiln.generation.validation import can_print_now

        assert any(SEPARATE in note for note in can_print_now(loose).get("notes", []))
        assert [i["type"] for i in can_print_now(floating)["issues"]].count("floating_regions") == 1

    def test_the_slice_estimate(self, loose, tmp_path, no_auth):
        from kiln.plugins.estimate_tools import _EstimateToolsPlugin
        from kiln.slicer import SliceResult
        from kiln.slicer_filament import resolve_slice_filament

        gcode = tmp_path / "out.gcode"
        gcode.write_text(";LAYER_CHANGE\nG1 X1 E1\n; filament used [g] = 4.2\n")
        sliced = SliceResult(
            success=True,
            output_path=str(gcode),
            slicer="prusa-slicer",
            message="Sliced",
            filament=resolve_slice_filament("PLA"),
        )
        tool = _register(_EstimateToolsPlugin)["slice_and_estimate"]
        with patch("kiln.slicer.slice_file", MagicMock(return_value=sliced)):
            resp = tool(input_path=loose, material="PLA")
        assert resp["success"], resp
        assert SEPARATE in resp["message"]

    def test_kiln_validate(self, loose):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        out = CliRunner().invoke(cli, ["validate", loose])
        assert out.exit_code == 0, out.output
        assert SEPARATE in out.output
