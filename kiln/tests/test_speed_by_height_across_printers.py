"""Speeds by height, given once for a run of copies across several printers.

A run that shares copies of one part across printers
(``plan_multi_copy_split``) takes ``speed_by_height`` once, for the whole
run.  It is kept on every job of the run and reaches each printer's own
slice:

* the person's own ranges reach every printer as they were given;
* ``"kiln"`` reaches each printer as an ask carrying that printer, the run's
  material and every printer of the run (``fleet``), so each printer has its
  own plan;
* what each printer's slice says about its ranges is kept on that job;
* a printer whose slice refuses the wish is said by name and not printed,
  and the run's other printers go on;
* nothing asked, nothing passed.

The slicer and Kiln's servers are faked at their boundaries
(``kiln.slicer.slice_file``, ``kiln.pipelines.reslice_and_print``); the
queue, the scheduler, the pipelines and the submit door are the real ones.
"""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiln import _pro_speed_plan_bridge as bridge
from kiln.events import EventBus
from kiln.queue import JobStatus, PrintQueue
from kiln.scheduler import JobScheduler
from kiln.slicer import SliceResult
from kiln.speed_by_height import SpeedByHeightError

RANGES = [{"from_mm": 0.0, "to_mm": 5.0, "speed_mm_s": 40.0}, {"from_mm": 5.0, "to_mm": 20.0, "speed_mm_s": 120.0}]
BLOCK = {"ranges": [{"from_mm": 0.0, "to_mm": 5.0, "outcome": "applied"}], "slicer_time_s": {"with": 600, "without": 540}}


def _cube(path: Path, size: float = 20.0) -> str:
    v = [
        (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
        (0, 0, size), (size, 0, size), (size, size, size), (0, size, size),
    ]
    faces = [
        (0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


# ---------------------------------------------------------------------------
# The ask carries every printer of the run
# ---------------------------------------------------------------------------


class TestTheAskNamesTheFleet:
    def test_kiln_carries_the_printer_the_material_and_the_fleet(self):
        ask = bridge.with_context("kiln", printer="bambu_a1", material="PETG", fleet=["A", "B"])
        assert ask == {"by": "kiln", "printer": "bambu_a1", "material": "PETG", "fleet": ["A", "B"]}

    def test_an_asks_own_fleet_keeps_its_printers_and_gains_the_doors(self):
        ask = bridge.with_context({"by": "kiln", "fleet": ["A", "B"]}, fleet=["B", "C"])
        assert ask["fleet"] == ["A", "B", "C"]

    def test_own_ranges_are_never_touched(self):
        assert bridge.with_context(RANGES, printer="bambu_a1", fleet=["A", "B"]) is RANGES


# ---------------------------------------------------------------------------
# The run keeps the wish when it is queued
# ---------------------------------------------------------------------------


@pytest.fixture
def submit_world(monkeypatch, tmp_path):
    """``_submit_fill`` over a kept plan for printers A and B, with the
    queueing step recorded instead of run."""
    import kiln.job_splitter as js
    from kiln import print_signoff

    part = _cube(tmp_path / "part.stl")
    record = {
        "copies": 10, "material": "petg",
        "fill": {"printers": [{"printer": "A", "now": 4}, {"printer": "B", "now": 3, "later": 3}, {"printer": "C"}]},
    }
    monkeypatch.setattr(js, "_fill_record", lambda plan_id: (part, record, "planned"))
    seen: dict[str, Any] = {}

    def _submit(plan_id, signoff, *, asks=None, keep_record=None):
        seen["asks"] = asks
        return {"success": True, "plan_id": plan_id, "queued": [{"printer": "A", "job_id": "j1", "copies": 4}]}

    monkeypatch.setattr(js, "submit_fill_plan", _submit)
    monkeypatch.setattr(print_signoff, "current", lambda: None)
    monkeypatch.setattr(print_signoff, "record_for", lambda _cur: None)
    return SimpleNamespace(part=part, seen=seen)


class TestTheRunKeepsTheWish:
    def test_own_ranges_are_kept_as_given(self, submit_world):
        from kiln.plugins.recovery_tools import _submit_fill

        out = _submit_fill(submit_world.part, "run-1", asks={}, speed_by_height=RANGES)
        assert out["success"] is True
        assert submit_world.seen["asks"]["speed_by_height"] == RANGES

    def test_kiln_is_kept_with_the_material_and_every_printer_of_the_run(self, submit_world):
        from kiln.plugins.recovery_tools import _submit_fill

        _submit_fill(submit_world.part, "run-1", asks={}, speed_by_height="kiln")
        assert submit_world.seen["asks"]["speed_by_height"] == {"by": "kiln", "material": "petg", "fleet": ["A", "B"]}

    def test_ranges_that_cannot_be_sliced_queue_nothing(self, submit_world):
        from kiln.plugins.recovery_tools import _submit_fill

        out = _submit_fill(submit_world.part, "run-1", asks={}, speed_by_height=[{"from_mm": 5, "to_mm": 1}])
        assert out["success"] is False
        assert "asks" not in submit_world.seen

    def test_nothing_asked_keeps_nothing(self, submit_world):
        from kiln.plugins.recovery_tools import _submit_fill

        _submit_fill(submit_world.part, "run-1", asks={})
        assert "speed_by_height" not in (submit_world.seen["asks"] or {})


# ---------------------------------------------------------------------------
# Each printer's slice gets the wish
# ---------------------------------------------------------------------------


@pytest.fixture
def fill_world(monkeypatch):
    """A real queue and scheduler, the print pipeline recorded."""
    import kiln.job_splitter as js
    import kiln.pipelines as pipelines

    q = PrintQueue()
    sched = JobScheduler(q, MagicMock(), EventBus())
    calls: list[dict[str, Any]] = []
    finished: list[tuple[str, Any]] = []

    def _pipeline(**kw):
        calls.append(kw)
        return SimpleNamespace(success=True, steps=[], message="")

    monkeypatch.setattr(pipelines, "reslice_and_print", _pipeline)
    monkeypatch.setattr(pipelines, "_target_printer_id", lambda pid, name: {"A": "bambu_a1", "B": "prusa_mk4"}.get(name))
    monkeypatch.setattr(js, "run_printers", lambda plan_id: {"A", "B"} if plan_id == "run-1" else set())
    monkeypatch.setattr(sched, "_finish_fill_job", lambda job, name, result, error: finished.append((name, error)))

    def _job(printer: str, wish: Any = None) -> Any:
        fill = {"model_path": "/kiln-home/queued_parts/part.stl", "copies": 3, "material": None}
        if wish is not None:
            fill["speed_by_height"] = wish
        job_id = q.submit(file_name="part.stl", printer_name=printer, submitted_by="multi_copy_print",
                          metadata={"fill": fill, "split_plan_id": "run-1"})
        return q.get_job(job_id)

    return SimpleNamespace(q=q, sched=sched, calls=calls, finished=finished, job=_job)


class TestEachPrintersSlice:
    def test_own_ranges_reach_every_printer_unchanged(self, fill_world):
        for name in ("A", "B"):
            fill_world.sched._run_fill_job(fill_world.job(name, RANGES), name)
        assert [c["speed_by_height"] for c in fill_world.calls] == [RANGES, RANGES]
        assert [e for _n, e in fill_world.finished] == [None, None]

    def test_kiln_reaches_each_printer_with_its_own_id_and_the_whole_fleet(self, fill_world):
        kept = {"by": "kiln", "material": "petg", "fleet": ["A", "B"]}
        for name in ("A", "B"):
            fill_world.sched._run_fill_job(fill_world.job(name, dict(kept)), name)
        asks = [c["speed_by_height"] for c in fill_world.calls]
        assert asks == [
            {"by": "kiln", "printer": "bambu_a1", "material": "petg", "fleet": ["A", "B"]},
            {"by": "kiln", "printer": "prusa_mk4", "material": "petg", "fleet": ["A", "B"]},
        ]

    def test_a_printer_moved_into_the_run_is_counted_in_the_fleet(self, fill_world, monkeypatch):
        import kiln.job_splitter as js

        # D took a moved plate after the run was queued; C slices now.
        monkeypatch.setattr(js, "run_printers", lambda plan_id: {"A", "B", "D"})
        fill_world.sched._run_fill_job(fill_world.job("C", {"by": "kiln", "fleet": ["A", "B"]}), "C")
        ask = fill_world.calls[0]["speed_by_height"]
        assert ask["fleet"] == ["A", "B", "D", "C"] and ask["printer"] == "C"

    def test_nothing_asked_passes_nothing(self, fill_world):
        fill_world.sched._run_fill_job(fill_world.job("A"), "A")
        assert "speed_by_height" not in fill_world.calls[0]


# ---------------------------------------------------------------------------
# What each printer's slice said is kept; a refusal is said for that printer
# ---------------------------------------------------------------------------


def _started(block: Any = None) -> SimpleNamespace:
    data: dict[str, Any] = {"copies": {"placed": 3}}
    if block is not None:
        data["speed_by_height"] = block
    return SimpleNamespace(success=True, message="started", steps=[
        SimpleNamespace(name="slice", success=True, data=data),
        SimpleNamespace(name="start_print", success=True, data={"file_name": "plate.gcode"}),
    ])


def _refused() -> SimpleNamespace:
    return SimpleNamespace(success=False, message="Slicing failed: Kiln could not reach its servers.", steps=[
        SimpleNamespace(name="slice", success=False, message="Slicing failed: Kiln could not reach its servers.",
                        data={"code": bridge.NO_PLAN, "error": "Kiln could not reach its servers."}),
    ])


class TestEachPrintersOutcome:
    def _two_jobs(self):
        q = PrintQueue()
        sched = JobScheduler(q, MagicMock(), EventBus())
        ids = {}
        for name in ("A", "B"):
            ids[name] = q.submit(file_name="part.stl", printer_name=name, submitted_by="multi_copy_print",
                                 metadata={"fill": {"model_path": "p.stl", "copies": 3, "speed_by_height": "kiln"},
                                           "split_plan_id": "run-1"})
            q.mark_starting(ids[name], printer_name=name)
            sched._active_jobs[ids[name]] = name
            sched._preparing.add(ids[name])
        return q, sched, ids

    def test_each_printers_ranges_are_kept_on_its_job(self):
        q, sched, ids = self._two_jobs()
        sched._finish_fill_job(q.get_job(ids["A"]), "A", _started(BLOCK), None)
        assert q.get_job(ids["A"]).metadata["speed_by_height"] == BLOCK

    def test_a_refusal_is_said_for_that_printer_and_the_others_go_on(self):
        q, sched, ids = self._two_jobs()
        sched._finish_fill_job(q.get_job(ids["A"]), "A", _refused(), None)
        sched._finish_fill_job(q.get_job(ids["B"]), "B", _started(BLOCK), None)
        refused = q.get_job(ids["A"])
        assert refused.status == JobStatus.FAILED
        said = refused.metadata["speed_by_height_refused"]
        assert said["printer"] == "A" and said["code"] == bridge.NO_PLAN
        assert "A" in said["message"] and "servers" in said["message"]
        assert ids["A"] not in sched._retry_counts
        assert q.get_job(ids["B"]).status == JobStatus.PRINTING


# ---------------------------------------------------------------------------
# The pipelines take it too, and say a refusal by its code
# ---------------------------------------------------------------------------


def _fake_slice(tmp_path: Path, block: Any = None) -> MagicMock:
    gcode = tmp_path / "out.gcode"
    gcode.write_text(";LAYER_CHANGE\nG1 X1 E1\n")
    result = SliceResult(success=True, output_path=str(gcode), slicer="prusa-slicer", message="Sliced")
    result.speed_by_height = block
    return MagicMock(return_value=result)


class TestThePipelines:
    @pytest.mark.parametrize("door", ["quick_print", "reslice_and_print"])
    def test_own_ranges_reach_the_slice_and_its_block_is_kept(self, door, tmp_path):
        import kiln.pipelines as pipelines

        stl = _cube(tmp_path / "cube.stl")
        spy = _fake_slice(tmp_path, BLOCK)
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.pipelines._resolve_pipeline_adapter", side_effect=RuntimeError("no printer")):
            result = getattr(pipelines, door)(
                model_path=stl, printer_id="ender3", skip_validation=True, speed_by_height=RANGES,
            )
        assert spy.call_args.kwargs["speed_by_height"] == RANGES
        step = next(s for s in result.steps if s.name == "slice")
        assert step.data["speed_by_height"] == BLOCK

    def test_a_refused_wish_fails_the_slice_with_its_code(self, tmp_path):
        from kiln.pipelines import reslice_and_print

        stl = _cube(tmp_path / "cube.stl")
        spy = MagicMock(side_effect=SpeedByHeightError("Kiln could not reach its servers.", code=bridge.NO_PLAN))
        with patch("kiln.slicer.slice_file", spy), \
                patch("kiln.pipelines._resolve_pipeline_adapter", side_effect=RuntimeError("no printer")):
            result = reslice_and_print(model_path=stl, printer_id="ender3", skip_validation=True, speed_by_height="kiln")
        step = next(s for s in result.steps if s.name == "slice")
        assert step.success is False and step.data["code"] == bridge.NO_PLAN
        assert spy.call_count == 1 and spy.call_args.kwargs["speed_by_height"]["by"] == "kiln"
        assert result.success is False
