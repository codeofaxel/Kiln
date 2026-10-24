"""The printability verdict says when a part prints as more than one piece.

A bookmark whose text was cut straight through came out as five solids, the
body and the middles of R, e, a and d, and the check said "Ready to print."
(2026-10-23): it never looked at the pieces.  Now every door that gives the
verdict says it: separate parts on the plate print, and each comes off on its
own; a piece resting on the part, or a clearance away from it, prints loose on
it; a piece in the air that nothing joins or holds means the part will not
print as it is, in the fusion check's words.  Pieces that share a surface or
sink into one another are one solid and are not mentioned.
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
        assert SEPARATE not in sentence

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
