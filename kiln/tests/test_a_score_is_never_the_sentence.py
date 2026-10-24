"""A score is never the sentence: a turn, a material pick, a printer's health.

A score is for comparing versions; it stays in the reply's data, and the
sentence a person reads says the outcome and what to do.  Until 2026-10-23
four doors said a score as the answer: ``auto_orient_model`` ("Best
orientation: rotate X=0.0deg Y=90.0deg (score 100.0).  Original score: 100.0.
Improvement: 0.0%."), the design audit's orientation gate ("Best orientation
improves score by 12.3%."), the material pick ("PETG scores highest for
'strong' intent (primary attribute: strength). Score: 62.6/100.", and on a
poor shelf "(68.5/100 vs 48.0/100)"), and the printer-health alert ("Printer
health score is 0%.").

Each now says what it found: what turning the part changes, in the units it
is measured in (``kiln.auto_orient.describe_orientation_change``, one wording
for both orientation doors); why a material wins and what the runner-up
would have given instead (``kiln.material_routing.describe_material_pick``);
what the printer's prints did.  Every reply is read by the real-export
corpus guard's own score reader (``tests._real_exports.scores_said``).
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402


def _call(name: str, **args):
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


@pytest.fixture()
def pillar(tmp_path) -> str:
    """A 10 mm square post 80 mm tall: lying down, it stands lower and more
    of it touches the bed."""
    path = tmp_path / "pillar.stl"
    _box((0, 0, 0), (10, 10, 80)).export(path)
    return str(path)


@pytest.fixture()
def plate(tmp_path) -> str:
    """A 20 x 20 x 5 mm plate lying flat: no turn helps."""
    path = tmp_path / "plate.stl"
    _box((0, 0, 0), (20, 20, 5)).export(path)
    return str(path)


@pytest.fixture()
def tee(tmp_path) -> str:
    """A post with a wide bar on top: upside down, the bar needs no support."""
    path = tmp_path / "tee.stl"
    trimesh.util.concatenate([_box((-5, -5, 0), (5, 5, 30)), _box((-20, -5, 30), (20, 5, 35))]).export(path)
    return str(path)


class TestTheTurnIsSaidInItsOwnUnits:
    def test_a_tall_part_is_told_what_lying_down_changes(self, pillar):
        reply = _call("auto_orient_model", file_path=pillar)
        assert reply.get("success"), reply
        assert rx.scores_said(reply) == []
        said = reply["message"]
        assert said.startswith("Turn it 90° about "), said
        assert "it stands lower (80.0 → 10.0 mm)" in said, said
        assert "more of it touches the bed (100 → 800 mm2)" in said, said
        assert said.endswith("Call again with apply=True to write the turned copy."), said
        # The scores stay in the data, for comparing versions.
        result = reply["result"]
        assert {"original_score", "improvement_percentage"} <= result.keys()
        assert isinstance(result["best"]["score"], float)

    def test_a_part_with_an_overhang_is_told_the_support_a_turn_saves(self, tee):
        said = _call("auto_orient_model", file_path=tee)["message"]
        assert "it needs less support (~" in said or "it needs no support (it needed ~" in said, said
        assert not rx._SAYS_A_SCORE.search(said), said

    def test_a_part_already_lying_flat_is_told_to_print_it_as_it_is(self, plate):
        reply = _call("auto_orient_model", file_path=plate)
        assert reply["message"] == "It already sits in its best orientation: print it as it is.", reply["message"]
        assert rx.scores_said(reply) == []

    def test_the_written_copy_is_named_instead_of_the_offer(self, pillar, tmp_path):
        out = str(tmp_path / "laid.stl")
        reply = _call("auto_orient_model", file_path=pillar, apply=True, output_path=out)
        assert rx.scores_said(reply) == []
        said = reply["message"]
        assert said.startswith("Turn it 90° about "), said
        assert said.endswith(f"Reoriented file: {out}") and "apply=True" not in said, said

    def test_the_design_audit_says_the_same_turn(self, pillar):
        """One wording for both orientation doors: the audit's gate says what
        auto_orient_model says, without its offer to write the copy."""
        audit = _call("audit_original_design", file_path=pillar, requirements="holds a pen")
        assert audit.get("success"), audit
        gate = next(g for g in audit["gates"] if g["name"] == "orientation_efficiency")
        assert rx.scores_said({"gates": [gate]}) == []
        orient = _call("auto_orient_model", file_path=pillar)["message"]
        assert orient.startswith(gate["message"]) and "it stands lower" in gate["message"], (gate, orient)
        assert "improvement_percentage" in gate["details"]

    def test_the_audit_warns_exactly_when_the_turn_it_names_gains_something(self, pillar, plate):
        """The gate's verdict is its own message's: a turn that gains
        something measurable is a warning, costs the readiness score its five
        points and is the next action, said the same way.  Until 2026-10-23
        the gate read the scores' percentage instead, which on the bed-contact
        floor is the same for every face of a block, so a pillar the message
        told to lie down passed."""
        audit = _call("audit_original_design", file_path=pillar, requirements="holds a pen")
        gate = next(g for g in audit["gates"] if g["name"] == "orientation_efficiency")
        assert gate["passed"] is False and gate["severity"] == "warning", gate
        assert gate["message"] in audit["next_actions"], audit["next_actions"]
        with patch("kiln.original_design.worth_turning", return_value=False):
            unturned = _call("audit_original_design", file_path=pillar, requirements="holds a pen")
        assert unturned["readiness_score"] - audit["readiness_score"] == 5, (unturned["readiness_score"], audit["readiness_score"])

        flat = _call("audit_original_design", file_path=plate, requirements="holds a pen")
        flat_gate = next(g for g in flat["gates"] if g["name"] == "orientation_efficiency")
        assert flat_gate["passed"] is True and flat_gate["severity"] == "info", flat_gate
        assert flat_gate["message"] not in flat["next_actions"]


class TestTheMaterialPickSaysWhyItWins:
    def test_the_pick_says_why_and_what_the_next_best_would_give(self):
        reply = _call("recommend_material", intent="make it strong")
        assert reply.get("success"), reply
        assert rx.scores_said(reply) == []
        rec = reply["recommendation"]
        pick, runner = rec["material"]["display_name"], rec["alternatives"][0]["material"]
        assert rec["reasoning"].startswith(f"{pick} is the best fit for a strong part: against {runner}, it "), rec
        assert "is stronger" in rec["reasoning"].split(". ")[0], rec["reasoning"]
        assert f"{runner}, the next best, " in rec["reasoning"], rec["reasoning"]
        # The scores stay in the data, for comparing materials.
        assert isinstance(rec["score"], float) and all("score" in alt for alt in rec["alternatives"])

    def test_a_poor_fit_on_hand_says_what_the_better_material_would_give(self, tmp_path):
        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "shelf.db"))
        try:
            db.save_material("a1", 0, "PLA", color="white", remaining_grams=900.0)
            with patch("kiln.persistence.get_db", return_value=db):
                reply = _call("recommend_material", intent="flexible gasket", on_hand_only=True, printer_id="a1")
        finally:
            db.close()
        assert reply.get("success"), reply
        assert rx.scores_said(reply) == []
        rec = reply["recommendation"]
        assert rec["reasoning"].startswith(
            "POOR FIT ON HAND: TPU suits a flexible part materially better, but you don't have it: "
            "against PLA, it is more flexible. PLA is the best of what you do have."
        ), rec["reasoning"]
        better = rec["availability"]["better_catalog_option"]
        assert better["score"] > rec["score"] and better["score_gap"] > 0


class TestPrinterHealthSaysWhatThePrintsDid:
    def test_a_failing_printer_is_told_what_its_prints_did(self, tmp_path):
        import time

        from kiln.persistence import KilnDB

        db = KilnDB(db_path=str(tmp_path / "history.db"))
        try:
            for i in range(10):
                db.save_print_record({
                    "job_id": f"fail-{i}", "printer_name": "ender3", "file_name": "benchy.gcode",
                    "status": "failed", "duration_seconds": 3600.0, "material_type": "PLA",
                    "created_at": time.time(), "completed_at": time.time(),
                })
            with patch("kiln.persistence.get_db", return_value=db):
                reply = _call("printer_trend_analysis", printer_name="ender3")
        finally:
            db.close()
        assert reply.get("success"), reply
        assert rx.scores_said(reply) == []
        report = reply["trend_report"]
        alert = next(a for a in report["alerts"] if a["category"] == "health")
        assert alert["message"].startswith("This printer needs attention: only 0% of its 10 prints in the last "), alert
        assert alert["message"].endswith("Inspect it for maintenance before the next long print."), alert
        assert report["health_score"] == 0.0
