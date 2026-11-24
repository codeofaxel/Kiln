"""The note on an edge plan names the finishes the plan beside it holds.

A plan may mix finishes: a chooser can answer a bevel for a chain the caller
asked to round.  The note is the sentence a person reads first, so it counts
each finish the edge list shows and never says every chain takes the one
finish that was asked for.  The plan note and the note on a finished part
share one wording, so the two cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from kiln.edge_finish import finish_part
from kiln.edge_plan import CHAMFER, FILLET


def _edge(edge_id: int, z: float, normals: list, **extra: Any) -> dict:
    return {
        "id": edge_id, "chain": edge_id, "curve": "line", "length_mm": 30.0, "mid": [15.0, 0.0, z],
        "tangent": [1.0, 0.0, 0.0], "z": [z, z], "turn_deg": 90.0, "corner": "outside", "faces": [1, 2],
        "normals": [normals] * 3, "room_mm": [20.0, 10.0], "across": [None, None],
        "ends": [edge_id * 10, edge_id * 10 + 1], **extra,
    }


#: A 30 x 20 x 10 block: one edge on the bed, one top rim, the mouth of a hole.
SURVEY = {
    "box": [0.0, 0.0, 0.0, 30.0, 20.0, 10.0], "volume_mm3": 6000.0, "holes_mm": [2.6],
    "edges": [
        _edge(1, 0.0, [[0, 0, -1], [0, -1, 0]]),
        _edge(2, 10.0, [[0, 0, 1], [0, -1, 0]]),
        _edge(3, 10.0, [[0, 0, 1], [0, 1, 0]], curve="circle", hole_mm=2.6, hole_edge="mouth"),
    ],
}


def _plan(tmp_path: Path, choose) -> dict:
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;\n", encoding="utf-8")
    with patch("kiln.cad_edge.survey_step", return_value=SURVEY), patch(
        "kiln.step_import._ocp_available", return_value=True,
    ):
        return finish_part(
            str(step), kind=FILLET, size_mm=1.5, plan_only=True, choose=choose,
            nozzle_mm=0.4, layer_height_mm=0.2,
        )


def _bevel_two(chains, _frame):
    """Answer a bevel for the edge on the bed and for the hole's mouth."""
    return {c["chain"]: (CHAMFER, 0.5, "a bevel prints here") for c in chains if c["chain"] in (1, 3)}


def _finishes(plan: dict) -> list[str]:
    return sorted(entry["finish"] for entry in plan["edges"]["finished"])


def test_a_mixed_plan_counts_each_finish_it_holds(tmp_path):
    plan = _plan(tmp_path, _bevel_two)
    assert _finishes(plan) == ["chamfer", "chamfer", "fillet"]
    note = plan["note"]
    assert "3 edge chains would be finished (1 rounded, 2 bevelled)" in note
    assert "would be rounded" not in note


def test_a_plan_of_one_finish_still_names_it(tmp_path):
    plan = _plan(tmp_path, None)
    assert set(_finishes(plan)) == {"fillet"}
    assert f"{len(plan['edges']['finished'])} edge chains would be rounded" in plan["note"]
    assert "finished (" not in plan["note"]


def test_the_note_on_a_finished_part_uses_the_same_words():
    from kiln.edge_finish import _done_note
    from kiln.edge_plan import EdgePlan

    mixed = EdgePlan(treatments=[{"kind": FILLET}, {"kind": CHAMFER}, {"kind": CHAMFER}])
    said = _done_note(mixed, FILLET, {"cad_source": "/tmp/part.step"}, "")
    assert said.startswith("3 edge chains finished (1 rounded, 2 bevelled) on the CAD file part.step.")
    same = EdgePlan(treatments=[{"kind": CHAMFER}, {"kind": CHAMFER}])
    assert _done_note(same, CHAMFER, {"cad_source": "/tmp/part.step"}, "").startswith("2 edge chains bevelled on")
