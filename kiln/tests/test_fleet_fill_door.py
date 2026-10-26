"""Copies of one part across the printers: ``plan_multi_copy_split`` (Kiln Business).

The share itself is worked out on Kiln's servers; here it is stood in for by a
canned ``fleet_fill/1`` document at ``kiln.server._pro_api_call``, the one
door every served answer comes through.  Pinned here:

* the door: Business, a mesh only, the plan kept under its id with a picture,
  a miss said in the shared voice with nothing planned;
* the go: one copies job per printer, pinned, each carrying the run's id and
  the person's yes; the first plate of an occupied printer carries the plate
  the person saw; asking twice queues nothing twice;
* the run: status follows every plate of it, the ones queued later included,
  and cancelling stops them all (it stopped none before: it asked the queue
  module for an accessor that does not exist);
* the queue: a run's first plate beside parts starts while the plate holds
  exactly what was planned beside, waits and says so when it changed, and the
  plate after it always waits for a cleared plate;
* the plate reading carries the copies, for a mesh only;
* the picture: drawn, printers that need the person first.
"""

from __future__ import annotations

import json
import os
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln.events import EventBus
from kiln.plate_state import PLATE_CHANGED_CODE, PlateJob, fingerprint, mark_clear, mark_occupied, read
from kiln.printers.base import JobProgress, PrinterCapabilities, PrinterState, PrinterStatus, PrintResult
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import JobScheduler

JAR = PlateJob(file="jar.gcode", footprint_mm=[71.0, 101.0, 185.0, 154.0], max_z_mm=20.0, printer_id="bambu_a1")


def _cube(path, w=30.0, d=30.0, h=8.0) -> str:
    v = [(0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0), (0, 0, h), (w, 0, h), (w, d, h), (0, d, h)]
    f = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(f)))
        for a, b, c in f:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _printer(name: str, model: str = "bambu_a1") -> MagicMock:
    a = MagicMock()
    type(a).name = PropertyMock(return_value=name)
    type(a).capabilities = PropertyMock(return_value=PrinterCapabilities())
    a.serial = f"SER{name}"
    a._printer_model = model
    a.declared_printer_model.return_value = model
    a.snapshot_source = None
    a.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE)
    a.get_job.return_value = JobProgress()
    a.start_print.return_value = PrintResult(success=True, message="started")
    return a


def _fill(_finish: dict | None = None, _share: str = "even", _untimed: tuple = (), **rows) -> dict:
    """A ``fleet_fill/1`` document: rows of (now, later, plates, status)."""
    printers = [
        {"printer": n, "now": now, "later": later, "later_plates": plates, "status": status,
         "start": "quiet_start" if status == "occupied" else "vendor_start", "spots": [[2.0 + 40 * i, 2.0] for i in range(now)]}
        for n, (now, later, plates, status) in rows.items()
    ]
    now = sum(p["now"] for p in printers)
    later = sum(p["later"] for p in printers)
    return {"schema": "fleet_fill/1", "wanted": now + later, "now": now, "later": later,
            "later_plates": sum(p["later_plates"] for p in printers), "unplaced": 0, "printers": printers,
            "not_used": [{"printer": "C", "status": "unknown", "why": ["Kiln does not know what is on C's plate"]}],
            "share": _share, "finish": _finish, "untimed": list(_untimed),
            "summary": f"{now} copies now; {later} more later."}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as emergency

    monkeypatch.setattr(emergency, "_coordinator", None)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    import kiln.server as srv
    from kiln import account_plan
    from kiln.persistence import KilnDB
    from kiln.plugins import recovery_tools

    reg, q = PrinterRegistry(), PrintQueue()
    monkeypatch.setattr(srv, "_get_registry", lambda: reg)
    monkeypatch.setattr(srv, "_get_queue", lambda: q)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    db = KilnDB(str(tmp_path / "kiln.db"))
    monkeypatch.setattr("kiln.persistence.get_db", lambda: db)
    tier = {"covers": True}
    monkeypatch.setattr(account_plan, "_covers", lambda needed: tier["covers"])
    # The plain install's own gate, whatever this machine has installed beside it.
    monkeypatch.setattr(srv, "requires_tier", account_plan.requires_tier)
    sent: list[dict] = []
    answer = {"value": None}

    def api(tool, **kw):
        sent.append({"tool": tool, **kw})
        if isinstance(answer["value"], Exception):
            raise answer["value"]
        return answer["value"]

    monkeypatch.setattr(srv, "_pro_api_call", api)

    class _MCP:
        tools: dict = {}

        def tool(self, *a, **k):
            def deco(fn):
                _MCP.tools[fn.__name__] = fn
                return fn
            return deco

    recovery_tools.plugin.register(_MCP())
    # The slicer is stood in for unless a test says otherwise: a plan's
    # times are pinned in TestHowLongTheRunTakes, and once for real below.
    monkeypatch.setattr(recovery_tools, "_sliced_seconds", lambda *a, **k: None)
    # Each test's stand-in slicer is its own world: times kept from another
    # test's stand-in are not this one's.
    monkeypatch.setattr(recovery_tools, "_model_time_cache", {}, raising=False)
    printers = {n: _printer(n) for n in ("A", "B", "C")}
    for n, a in printers.items():
        reg.register(n, a)
    mark_clear(printers["A"], "human")
    mark_occupied(printers["B"], JAR)
    part = _cube(tmp_path / "clip.stl")
    return SimpleNamespace(srv=srv, q=q, reg=reg, tools=_MCP.tools, printers=printers, part=part, sent=sent,
                           answer=answer, tier=tier)


def _plan(world, copies=40, _finish=None, _share="even", _untimed=(), share="soonest", **rows):
    rows = rows or {"A": (24, 10, 1, "clear"), "B": (6, 0, 0, "occupied")}
    world.answer["value"] = {"success": True, "fill": _fill(_finish, _share, _untimed, **rows)}
    return world.tools["plan_multi_copy_split"](world.part, copies, material="PLA", share=share)


def _go(world, plan_id, monkeypatch):
    monkeypatch.setattr(world.srv, "_preview_gate_error", lambda *a, **k: None)
    return world.tools["plan_multi_copy_split"](world.part, 40, plan_id=plan_id, submit=True)


class TestThePlan:
    def test_below_business_it_is_not_run(self, world):
        world.tier["covers"] = False
        out = _plan(world)
        assert out["success"] is False and out["required_tier"] == "business"
        assert world.sent == []

    def test_business_gets_the_plan_kept_and_drawn(self, world):
        out = _plan(world)
        assert out["success"] is True, out
        assert out["plan"]["now"] == 30 and out["plan_id"]
        assert out["picture"] and os.path.getsize(out["picture"]) > 1000
        assert "submit=True" in out["message"]
        sent = world.sent[0]
        assert sent["tool"] == "plan_fleet_fill" and sent["copies"] == 40
        assert {c["printer_id"] for c in sent["candidates"]} == {"A", "B", "C"}
        assert all(r["request"]["copies"] == 40 for r in sent["plates"].values() if r["request"])
        assert "SERA" not in json.dumps(sent["plates"]), "no machine serial travels"
        assert all(r["request"]["serial"].startswith("id:") for r in sent["plates"].values() if r["request"])

    def test_a_sliced_file_is_refused_before_anything_is_asked(self, world, tmp_path):
        gcode = tmp_path / "clip.gcode"
        gcode.write_text("G28\n")
        out = world.tools["plan_multi_copy_split"](str(gcode), 10, material="PLA")
        assert out["success"] is False and "mesh" in out["error"]["message"]
        assert world.sent == []

    def test_a_miss_plans_and_queues_nothing(self, world):
        world.answer["value"] = ConnectionRefusedError("down")
        out = world.tools["plan_multi_copy_split"](world.part, 10, material="PLA")
        assert out["success"] is False and out["error"]["code"] == "FLEET_FILL_UNAVAILABLE"
        assert "won't plan or queue any copies" in out["error"]["message"]
        assert "multi_copy_print" in out["error"]["message"]
        assert world.q.list_jobs() == []


class TestTheGo:
    def test_one_pinned_job_per_printer_carrying_the_run(self, world, monkeypatch):
        plan = _plan(world)
        out = _go(world, plan["plan_id"], monkeypatch)
        assert out["success"] is True, out
        jobs = {j.printer_name: j for j in world.q.list_jobs()}
        assert set(jobs) == {"A", "B"}
        assert jobs["A"].metadata["fill"]["copies"] == 34 and jobs["B"].metadata["fill"]["copies"] == 6
        assert all(j.metadata["split_plan_id"] == plan["plan_id"] for j in jobs.values())
        assert "beside_plate" not in jobs["A"].metadata["fill"]
        assert jobs["B"].metadata["fill"]["beside_plate"] == fingerprint(read(world.printers["B"]))
        assert out["message"].startswith("Queued 40 copies on 2 printers.")

    def test_a_yes_that_named_no_printer_is_written_as_a_yes_for_these_printers(self, world, monkeypatch):
        """The run's own question names no single printer; the jobs carry it
        as a yes for exactly the run's printers, so the queue refuses any
        other at the start.  A/B: without the stamp the scope stays empty and
        printer C is covered -- "assert None is not None"."""
        from kiln import print_signoff

        plan = _plan(world)
        monkeypatch.setattr(print_signoff, "record_for", lambda current: {"tool": "plan_multi_copy_split", "scope": None,
                                                                         "printer_name": None, "source": "elicited"})
        _go(world, plan["plan_id"], monkeypatch)
        jobs = world.q.list_jobs()
        assert {tuple(j.metadata["preview_signoff"]["scope"]) for j in jobs} == {("A", "B")}
        assert print_signoff.record_refusal(jobs[0].metadata["preview_signoff"], "C") is not None
        assert print_signoff.record_refusal(jobs[0].metadata["preview_signoff"], "B") is None

    def test_asking_twice_queues_nothing_twice(self, world, monkeypatch):
        plan = _plan(world)
        _go(world, plan["plan_id"], monkeypatch)
        again = _go(world, plan["plan_id"], monkeypatch)
        assert again["already"] == "submitted" and len(world.q.list_jobs()) == 2

    def test_the_go_takes_the_preview_gate(self, world, monkeypatch):
        plan = _plan(world)
        out = world.tools["plan_multi_copy_split"](world.part, 40, plan_id=plan["plan_id"], submit=True)
        assert out["success"] is False and world.q.list_jobs() == []


class TestTheRun:
    def test_status_and_cancel_follow_every_plate_queued_later(self, world, monkeypatch):
        """A/B: with ``_queue_rest`` keeping only the yes (the earlier
        ``("preview_signoff",)``), the later plate is not the run's: its
        copies are not counted -- "assert (True is True and 40 == 50)"."""
        plan = _plan(world)
        _go(world, plan["plan_id"], monkeypatch)
        first = next(j for j in world.q.list_jobs() if j.printer_name == "A")
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._queue_rest(first, 10)
        later = [j for j in world.q.list_jobs() if j.id != first.id and j.printer_name == "A"][0]
        st = world.tools["split_plan_status"](plan["plan_id"])
        assert st["success"] is True and st["progress"]["copies_to_come"] == 50
        assert st["message"] == "0 of 40 copies printed: 0 plates printed, 0 printing, 3 waiting; 50 copies still to come."
        out = world.tools["cancel_split_plan"](plan["plan_id"])
        assert out["cancelled"] == 3
        assert world.q.get_job(later.id).status == JobStatus.CANCELLED

    def test_cancelling_a_spread_plan_cancels_its_jobs(self, world, monkeypatch):
        """A/B: with the old ``from kiln.queue import get_queue`` back, every
        part reports "Failed to cancel" and nothing is cancelled -- "assert (0 == 2)"."""
        from kiln.job_splitter import SplitJob, SplitPlan, cancel_split_plan, submit_split_plan

        monkeypatch.setattr(world.srv, "_preview_gate_error", lambda *a, **k: None)
        parts = [SplitJob(part_id=f"p{i}", file_path="x.gcode", printer_name=n, printer_model="", estimated_time_seconds=0,
                          material="PLA", settings={}, status="pending") for i, n in enumerate(("A", "B"))]
        plan = SplitPlan(original_file="x.gcode", split_type="multi_copy", parts=parts, total_printers=2,
                         estimated_total_time_seconds=0, estimated_sequential_time_seconds=0, time_savings_percentage=0,
                         assembly_instructions="")
        plan_id = submit_split_plan(plan)
        out = cancel_split_plan(plan_id)
        assert out["cancelled_count"] == 2 and out["errors"] == [], out
        assert all(j.status == JobStatus.CANCELLED for j in world.q.list_jobs())


class TestTheQueue:
    def _beside_job(self, world, plate_print):
        mark_occupied(world.printers["B"], JAR)
        seen = fingerprint(read(world.printers["B"]))
        if plate_print is not None:
            mark_occupied(world.printers["B"], plate_print)
        out = world.srv._queue_fill_rest(world.part, 6, printer_name="B", printer_id=None, overrides=None,
                                         slicer_path=None, spacing_mm=10.0, signoff=None, beside_plate=seen)
        return out["job_id"]

    def test_a_first_plate_beside_parts_starts_while_the_plate_is_as_planned(self, world):
        """A/B: with the ``beside_plate`` branch in ``_plate_answer`` removed,
        the job waits for the plate -- "assert [] == ['B']"."""
        job = self._beside_job(world, None)
        sched = JobScheduler(world.q, world.reg, EventBus())
        started: list[str] = []
        sched._spawn = lambda work: None
        real = sched._start_fill_job
        sched._start_fill_job = lambda j, p: (started.append(p), real(j, p))
        sched.tick()
        assert started == ["B"]
        assert world.q.get_job(job).metadata.get("waiting") is None

    def test_a_changed_plate_holds_it_and_says_so(self, world):
        job = self._beside_job(world, PlateJob(file="vase.gcode", footprint_mm=[10.0, 10.0, 50.0, 50.0], max_z_mm=60.0))
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._spawn = lambda work: None
        sched.tick()
        note = world.q.get_job(job).metadata["waiting"]
        assert note["kind"] == "plate" and note["code"] == PLATE_CHANGED_CODE
        assert "planned beside what was on B's plate when you saw the plan" in note["sentence"]
        assert world.printers["B"].start_print.call_count == 0

    def test_the_plate_after_it_waits_for_a_cleared_plate(self, world):
        job = self._beside_job(world, None)
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._queue_rest(world.q.get_job(job), 4)
        rest = [j for j in world.q.list_jobs() if j.id != job][0]
        assert "beside_plate" not in rest.metadata["fill"] and rest.metadata["fill"]["copies"] == 4


def test_the_plate_reading_carries_the_copies_for_a_mesh_only(world, tmp_path):
    from kiln import _pro_placement_bridge as bridge

    mesh = bridge.plate_reading("A", world.printers["A"], {**bridge.job_envelope(world.part), "copies": 12})
    assert mesh["request"]["copies"] == 12
    gcode = tmp_path / "x.gcode"
    gcode.write_text("G1 X1 Y1\n")
    sliced = bridge.plate_reading("A", world.printers["A"], {**bridge.job_envelope(str(gcode)), "copies": 12})
    assert sliced["request"]["copies"] is None


def test_the_picture_puts_the_printers_that_need_the_person_first():
    from kiln.fleet_fill_picture import _sections, draw

    plan = _fill(A=(3, 0, 0, "clear"))
    plan["not_used"].append({"printer": "D", "status": "clear", "why": ["the other printers take every copy"]})
    assert [name for name, _rows in _sections(plan)] == ["Needs you", "Printing", "Not needed for this run"]
    path = draw(plan, part_size_mm=[30, 30, 8], beds={"A": [256, 256], "C": [256, 256], "D": None}, models={"A": "bambu_a1"})
    from PIL import Image

    assert Image.open(path).size[0] >= 300


# --- defects a lap found (2026-10-26; each fails at 27a834df) ---


def test_the_picture_never_strands_a_date_s_day_on_its_own_line():
    """The lap saw "since Oct" end one line of a printer's note and "26"
    open the next; the suite only checked the picture was drawn."""
    from PIL import Image, ImageDraw

    from kiln._fonts import load_font
    from kiln.fleet_fill_picture import _wrap

    d, font = ImageDraw.Draw(Image.new("RGB", (1, 1))), load_font(15)
    text = "A1-06 is marked needs repair since Oct 26"
    width = d.textlength("A1-06 is marked needs repair since Oct", font=font) + 2
    lines = _wrap(d, text, font, width)
    assert lines[-1].endswith("Oct 26") and " ".join(lines) == text, lines
    text = "Kiln found no spot on this plate for a 30 mm by 30 mm part beside vase"
    width = d.textlength("Kiln found no spot on this plate for a 30", font=font) + 2
    lines = _wrap(d, text, font, width)
    assert not any(line.split()[-1].isdigit() for line in lines) and " ".join(lines) == text, lines


class TestTheGoIsForThePartSeen:
    def test_another_part_cannot_be_queued_under_this_plan(self, world, monkeypatch, tmp_path):
        """A/B: with the ``_same_file`` check removed, the jobs are queued
        for the kept part on a yes given for another -- "assert 2 == 0"."""
        plan = _plan(world)
        other = _cube(tmp_path / "other.stl", w=12.0)
        monkeypatch.setattr(world.srv, "_preview_gate_error", lambda *a, **k: None)
        out = world.tools["plan_multi_copy_split"](other, 40, plan_id=plan["plan_id"], submit=True)
        assert out["success"] is False and out["error"]["code"] == "PLAN_PART_MISMATCH"
        assert len(world.q.list_jobs()) == 0

    def test_a_yes_for_one_printer_queues_nothing(self, world, monkeypatch):
        from kiln import print_signoff

        plan = _plan(world)
        monkeypatch.setattr(world.srv, "_preview_gate_error", lambda *a, **k: None)
        monkeypatch.setattr(print_signoff, "record_for", lambda current: {"scope": "printer", "printer_name": "A"})
        out = world.tools["plan_multi_copy_split"](world.part, 40, plan_id=plan["plan_id"], submit=True)
        assert out["success"] is False and out["error"]["code"] == "CONSENT_SCOPE", out
        assert "not started on B" in out["error"]["message"]
        assert world.q.list_jobs() == []


# ---------------------------------------------------------------------------
# The old fleet doors, retired into this one
# ---------------------------------------------------------------------------


class TestTheOldFleetDoors:
    def _fleet_tools(self, world):
        from kiln.plugins import fleet_tools

        class _MCP:
            tools: dict = {}

            def tool(self, *a, **k):
                def deco(fn):
                    _MCP.tools[fn.__name__] = fn
                    return fn
                return deco

        fleet_tools.plugin.register(_MCP())
        return _MCP.tools

    def test_fleet_submit_job_points_here_and_queues_nothing(self, world):
        tools = self._fleet_tools(world)
        out = tools["fleet_submit_job"](world.part, printer_name="A")
        assert out["success"] is False and out["error"]["code"] == "RETIRED"
        assert "plan_multi_copy_split" in out["error"]["message"] and "Nothing was queued" in out["error"]["message"]
        assert world.q.list_jobs() == []
        assert world.printers["A"].get_state.call_count == 0, "it reads no printer"

    def test_fleet_job_status_points_at_the_run_status(self, world):
        out = self._fleet_tools(world)["fleet_job_status"]("abc")
        assert out["error"]["code"] == "RETIRED" and "split_plan_status" in out["error"]["message"]

    def test_fleet_utilization_counts_the_real_queue(self, world):
        """A/B: with the old orchestrator branch back, the job figures came
        from a table nothing fills (or were None) -- "assert None == {...}"."""
        world.q.submit("a.gcode", printer_name="A")
        world.q.submit("b.gcode", printer_name="B")
        out = self._fleet_tools(world)["fleet_utilization"]()
        assert out["success"] is True
        assert out["utilization"]["job_metrics"] == {"queued": 2, "running": 0, "completed": 0, "failed": 0}
        assert out["utilization"]["total_printers"] == 3


# ---------------------------------------------------------------------------
# A failed plate's copies go back in the run
# ---------------------------------------------------------------------------


class TestAFailedPlate:
    def _printing_fill_job(self, world, copies=8, retries=0):
        from kiln.printers.base import JobResult

        job = world.srv._queue_fill_rest(world.part, copies, printer_name="A", printer_id=None, overrides=None,
                                         slicer_path=None, spacing_mm=10.0, signoff=None,
                                         run={"split_plan_id": "run-1"})["job_id"]
        if retries:
            fill = {**world.q.get_job(job).metadata["fill"], "failed_retries": retries}
            world.q.update_metadata(job, {"fill": fill})
        world.q.mark_starting(job, printer_name="A")
        world.q.mark_printing(job)
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._active_jobs[job] = "A"
        sched._seen_printing.add(job)
        a = world.printers["A"]

        def ends(result):
            a.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE,
                                                    last_job_result=getattr(JobResult, result.upper()))
            sched.tick()
        return job, sched, ends

    def _others(self, world, job):
        return [j for j in world.q.list_jobs() if j.id != job]

    def test_a_failed_plate_is_queued_again_for_a_cleared_plate(self, world):
        """A/B: with ``_settle_fill_plate`` not called, nothing is queued
        again and the run is 8 copies short -- "assert [] == [8]"."""
        job, _sched, ends = self._printing_fill_job(world)
        ends("failed")
        again = self._others(world, job)
        assert [j.metadata["fill"]["copies"] for j in again] == [8]
        assert again[0].metadata["fill"]["failed_retries"] == 1 and again[0].metadata["split_plan_id"] == "run-1"
        assert again[0].printer_name == "A" and "beside_plate" not in again[0].metadata["fill"]
        assert world.q.get_job(job).metadata["ended"] == "failed"

    def test_a_good_plate_and_a_cancel_are_not_reprinted(self, world):
        for ending in ("completed", "cancelled"):
            job, _sched, ends = self._printing_fill_job(world)
            ends(ending)
            assert world.q.get_job(job).metadata["ended"] in ("success", "cancelled"), ending
            assert not [j for j in world.q.list_jobs() if (j.metadata.get("fill") or {}).get("failed_retries")], ending

    def test_after_two_retries_it_stops_and_says_so(self, world):
        job, _sched, ends = self._printing_fill_job(world, retries=2)
        ends("failed")
        assert [j for j in self._others(world, job) if j.status == JobStatus.QUEUED] == []
        assert "did not queue its copies" in world.q.get_job(job).metadata["not_requeued"]

    def test_a_started_plate_means_its_own_copies(self, world):
        """A retry reprints the copies ON the plate, never the ones already
        queued after it.  A/B: without the metadata update, the job still
        asks for 20 -- "assert 20 == 12"."""
        job = world.srv._queue_fill_rest(world.part, 20, printer_name="A", printer_id=None, overrides=None,
                                         slicer_path=None, spacing_mm=10.0, signoff=None)["job_id"]
        sched = JobScheduler(world.q, world.reg, EventBus())
        world.q.mark_starting(job, printer_name="A")
        slice_step = SimpleNamespace(name="slice", success=True, data={"copies": {"placed": 12}})
        start = SimpleNamespace(name="start_print", success=True, data={"file_name": "plate.gcode"})
        sched._preparing.add(job)
        sched._finish_fill_job(world.q.get_job(job), "A", SimpleNamespace(success=True, steps=[slice_step, start]), None)
        assert world.q.get_job(job).metadata["fill"]["copies"] == 12
        rest = self._others(world, job)
        assert [j.metadata["fill"]["copies"] for j in rest] == [8]


# ---------------------------------------------------------------------------
# How long the run keeps the busiest printer printing
# ---------------------------------------------------------------------------


class TestHowLongTheRunTakes:
    def _fake_slicer(self, monkeypatch, times):
        """The slicer stood in: (file is a pair?, model) -> seconds."""
        from kiln.plugins import recovery_tools

        sliced: list[tuple[str, str]] = []

        def fake(file_path, model, printer, material):
            pair = "_multi" in os.path.basename(file_path)
            sliced.append((model, "pair" if pair else "one"))
            return times.get((model, pair))

        monkeypatch.setattr(recovery_tools, "_sliced_seconds", fake)
        return sliced

    def test_each_model_is_sliced_once_and_every_printer_s_time_goes_with_the_readings(self, world, monkeypatch):
        """One copy 1000 s, two copies 1600 s on an A1: each copy adds 600 s
        and each plate costs 400 s once, and B is still printing for an hour.

        A/B: with the plate's fixed part dropped (each copy counted at the
        one-copy time), A reads 1000 a copy -- "assert {... 1000.0 ...} == {... 600.0 ...}"."""
        sliced = self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        world.printers["B"].get_job.return_value = JobProgress(print_time_left_seconds=3600)
        out = _plan(world, 40, A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        assert sorted(sliced) == [("bambu_a1", "one"), ("bambu_a1", "pair")], "one model, sliced once each way"
        sent = world.sent[-1]
        assert sent["share"] == "soonest"
        each = {"per_copy_seconds": 600.0, "per_plate_seconds": 400.0}
        assert sent["times"] == {
            "A": {**each, "busy_seconds": 0.0}, "B": {**each, "busy_seconds": 3600.0}, "C": {**each, "busy_seconds": 0.0},
        }
        assert out["finish"] is None, "the canned plan carries no finish, and none is made up here"

    def test_the_finish_says_both_shares(self, world, monkeypatch):
        """Shared for the soonest finish the busiest printer is done in 5h 15m;
        shared evenly, 10h 10m and two fewer plates to clear."""
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        finish = {"seconds": 18900.0, "busiest": "A", "printers_seconds": {"A": 18900.0, "B": 18300.0}, "plates": 5,
                  "other": {"share": "even", "seconds": 36600.0, "plates": 3}}
        out = _plan(world, 40, finish, "soonest", A=(10, 20, 2, "clear"), B=(10, 0, 0, "occupied"))
        assert out["finish"]["sentence"] == (
            "About 5h 15m of printing on the busiest printer (A), plus clearing its plate twice in between, by the "
            "slicer's own estimate for each printer model. That is the soonest finish; shared evenly it would take "
            "about 10h 10m, with 2 fewer plates to clear."
        )
        assert out["finish"]["sentence"] in out["message"] and out["finish"]["printers_seconds"] == finish["printers_seconds"]
        assert world.sent[-1]["share"] == "soonest"

    def test_even_on_request_says_what_the_faster_printers_would_save(self, world, monkeypatch):
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        finish = {"seconds": 36600.0, "busiest": "B", "printers_seconds": {"A": 12600.0, "B": 36600.0}, "plates": 4,
                  "other": {"share": "soonest", "seconds": 18900.0, "plates": 5}}
        out = _plan(world, 40, finish, "even", share="even", A=(10, 10, 1, "clear"), B=(10, 10, 1, "occupied"))
        assert world.sent[-1]["share"] == "even"
        assert out["finish"]["sentence"].endswith(
            "Shared evenly, as asked; giving the faster printers more would finish in about 5h 15m, "
            "with 1 more plate to clear."
        )

    def test_a_model_the_slicer_cannot_do_is_named_and_never_guessed(self, world, monkeypatch):
        world.printers["B"].declared_printer_model.return_value = "bambu_p1s"
        world.printers["B"]._printer_model = "bambu_p1s"
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        finish = {"seconds": 6400.0, "busiest": "A", "printers_seconds": {"A": 6400.0}, "plates": 2, "other": None}
        out = _plan(world, 40, finish, "even", ("B",), A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        assert "B" not in world.sent[-1]["times"] and out["finish"]["untimed_models"] == ["bambu_p1s"]
        assert out["finish"]["sentence"] == (
            "About 1h 46m of printing on the busiest printer Kiln could time (A), by the slicer's own estimate for "
            "each printer model. Kiln has no time for Bambu P1S, so the copies are shared evenly."
        )

    def test_with_no_slicer_the_plan_carries_no_time(self, world, monkeypatch):
        self._fake_slicer(monkeypatch, {})
        out = _plan(world)
        assert world.sent[-1]["times"] == {}
        assert out["finish"] is None and "About" not in out["message"]

    def test_a_share_kiln_does_not_have_is_refused_before_anything_is_sliced(self, world, monkeypatch):
        sliced = self._fake_slicer(monkeypatch, {})
        out = world.tools["plan_multi_copy_split"](world.part, 40, material="PLA", share="fastest")
        assert out["success"] is False and out["error"]["code"] == "VALIDATION_ERROR"
        assert sliced == [] and world.sent == []


    def test_a_part_changed_since_is_sliced_again(self, world, monkeypatch):
        """Times are kept for the part's own bytes: a part changed since is
        timed afresh, never given the old part's times."""
        sliced = self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        _plan(world, 40)
        _cube(world.part, w=40.0)
        _plan(world, 40)
        assert len(sliced) == 4

    # --- defects a lap found (2026-10-26; each fails at 27a834df) ---

    def test_a_part_that_is_not_there_is_said_plainly_and_nothing_is_asked(self, world, monkeypatch, tmp_path):
        """The lap found a missing STL answered "send the part, not a sliced
        file": the servers read no part and took it for G-code."""
        sliced = self._fake_slicer(monkeypatch, {})
        out = world.tools["plan_multi_copy_split"](str(tmp_path / "gone.stl"), 40, material="PLA")
        assert out["success"] is False and out["error"]["code"] == "FILE_NOT_FOUND"
        assert "no part at" in out["error"]["message"] and "sliced" not in out["error"]["message"]
        assert world.sent == [] and sliced == []
        plan_id = _plan(world)["plan_id"]
        gone = world.tools["plan_multi_copy_split"](str(tmp_path / "gone.stl"), 40, plan_id=plan_id, submit=True)
        assert gone["error"]["code"] == "FILE_NOT_FOUND" and world.q.list_jobs() == []

    def test_asking_again_for_the_other_share_does_not_slice_again(self, world, monkeypatch):
        """The lap found every plan sliced every model afresh, so switching
        to share="even" right after seeing the plan paid the whole slicing
        time again.  The suite missed it because each test planned once."""
        sliced = self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        _plan(world, 40, A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        _plan(world, 40, share="even", A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        assert sorted(sliced) == [("bambu_a1", "one"), ("bambu_a1", "pair")], "sliced once for both asks"
        assert world.sent[-1]["times"] == world.sent[-2]["times"] and world.sent[-1]["share"] == "even"

    def test_with_no_slicer_the_plan_says_why_it_shared_evenly(self, world, monkeypatch):
        """The lap found a plan with no slicer time at all shared the copies
        evenly and said nothing about it, though the soonest was asked for."""
        self._fake_slicer(monkeypatch, {})
        out = _plan(world, 40, None, "even", ("A", "B"))
        assert out["finish"] is None
        assert (
            "Kiln could not slice the part here for any of these printers, so the copies are shared evenly "
            "and no finish time is given." in out["message"]
        )

    def test_the_no_time_sentence_names_models_as_people_say_them_and_printers_with_none(self, world, monkeypatch):
        """The lap found the sentence naming raw model ids (``bambu_p1s``) and
        "a printer with no model set" without saying which printer."""
        world.printers["B"].declared_printer_model.return_value = "bambu_p1s"
        world.printers["B"]._printer_model = "bambu_p1s"
        world.printers["C"].declared_printer_model.return_value = None
        world.printers["C"]._printer_model = None
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        finish = {"seconds": 6400.0, "busiest": "A", "printers_seconds": {"A": 6400.0}, "plates": 2, "other": None}
        out = _plan(world, 40, finish, "even", ("B", "C"), A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        assert out["finish"]["sentence"].endswith(
            "Kiln has no time for Bambu P1S or C (no printer model set), so the copies are shared evenly."
        )
        assert out["finish"]["no_model_printers"] == ["C"]

    def test_a_printer_whose_time_left_could_not_be_used_is_named_for_that(self, world, monkeypatch):
        """The lap found a printer the servers left untimed for an odd
        time-left reading would have been named as a model the slicer could
        not do, though its model sliced fine."""
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        world.printers["B"].get_job.return_value = JobProgress(print_time_left_seconds=-5)
        finish = {"seconds": 6400.0, "busiest": "A", "printers_seconds": {"A": 6400.0}, "plates": 2, "other": None}
        out = _plan(world, 40, finish, "even", ("B",), A=(10, 0, 0, "clear"), B=(6, 4, 1, "occupied"))
        assert out["finish"]["sentence"].endswith(
            "Kiln has no time for B (its time left could not be read), so the copies are shared evenly."
        )
        assert out["finish"]["untimed_models"] == []

    def test_a_model_only_on_printers_the_plan_does_not_use_is_not_named(self, world, monkeypatch):
        """The lap found the sentence naming every registered model the slicer
        could not do, including ones on printers the plan left out entirely."""
        world.printers["C"].declared_printer_model.return_value = "bambu_p1s"
        world.printers["C"]._printer_model = "bambu_p1s"
        self._fake_slicer(monkeypatch, {("bambu_a1", False): 1000.0, ("bambu_a1", True): 1600.0})
        finish = {"seconds": 6400.0, "busiest": "A", "printers_seconds": {"A": 6400.0, "B": 3600.0}, "plates": 2,
                  "other": {"share": "even", "seconds": 7000.0, "plates": 2}}
        out = _plan(world, 40, finish, "soonest", A=(10, 0, 0, "clear"), B=(6, 0, 0, "occupied"))
        assert "p1s" not in out["message"].lower() and out["finish"]["untimed_models"] == []


@pytest.mark.skipif(not __import__("shutil").which("prusa-slicer") and not os.path.exists(
    "/Applications/PrusaSlicer.app"), reason="needs PrusaSlicer")
def test_models_sliced_side_by_side_each_get_their_own_time(tmp_path):
    """Two models sliced at once each get the time their own slice gives.

    A/B: with every slice writing to the slicer's shared folder (no
    ``output_dir``), the models read each other's files -- measured
    2026-10-26: three models at once, one answered with another's time and
    two answered nothing."""
    from kiln.plugins.recovery_tools import _model_times, _sliced_seconds

    part = _cube(tmp_path / "clip.stl")
    together = _model_times(part, {"bambu_a1": None, "prusa_mk4": None}, "PLA")
    for model in ("bambu_a1", "prusa_mk4"):
        alone = _sliced_seconds(part, model, None, "PLA")
        per_copy, per_plate = together[model]
        assert alone and abs((per_copy + per_plate) - alone) <= 1.0, (model, together, alone)
        assert per_copy > 60, together
    assert together["bambu_a1"] != together["prusa_mk4"]


# ---------------------------------------------------------------------------
# The person is asked about the run itself
# ---------------------------------------------------------------------------


class TestTheRunIsAsked:
    def test_the_plan_step_asks_nothing(self, world):
        import asyncio

        assert asyncio.run(world.srv._ask_for_a_run("plan_multi_copy_split", {"file_path": world.part}, object())) is None

    def test_the_go_asks_about_the_part_the_copies_and_the_printers(self, world, monkeypatch):
        """A/B: with plan_multi_copy_split off the consent map, the go is
        never asked about and no yes is recorded -- "TypeError: expected an
        instance of Token, got None"."""
        import asyncio

        from kiln import print_consent

        plan = _plan(world)
        asked: list[str] = []

        async def confirm(ctx, message, *, offer_window=True, offer_fleet=False):
            asked.append(message)
            assert offer_window is False and offer_fleet is False, "a window for one printer never stands in for a run"
            return print_consent.DialogAnswer(action="accept")

        monkeypatch.setattr(world.srv, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setattr(world.srv, "ask_user_to_confirm", confirm)
        async def ask_and_read():
            token = await world.srv._obtain_print_consent(
                "plan_multi_copy_split", {"file_path": world.part, "plan_id": plan["plan_id"], "submit": True}, object(),
            )
            current = print_consent._current.get()
            print_consent._current.reset(token)
            return current

        current = asyncio.run(ask_and_read())
        assert len(asked) == 1 and "on 2 printers (A, B)" in asked[0] and "40 in all: 30 now, 10 later" in asked[0]
        assert current is not None and current.printer_name is None, "the yes covers the run's printers"

    def test_a_no_queues_nothing(self, world, monkeypatch):
        import asyncio

        from kiln import print_consent

        plan = _plan(world)

        async def decline(ctx, message, **kw):
            return print_consent.DialogAnswer(action="decline")

        monkeypatch.setattr(world.srv, "host_can_ask_the_user", lambda mcp, ctx: True)
        monkeypatch.setattr(world.srv, "ask_user_to_confirm", decline)
        with pytest.raises(RuntimeError, match="the run was declined. Nothing was queued."):
            asyncio.run(world.srv._obtain_print_consent(
                "plan_multi_copy_split", {"file_path": world.part, "plan_id": plan["plan_id"], "submit": True}, object(),
            ))


# ---------------------------------------------------------------------------
# A failed plate of a fleet run asks where its copies go (Kiln Business)
# ---------------------------------------------------------------------------


def _verdict(decision: str, printer: str | None, copies: int = 24, **over) -> dict:
    return {"schema": "failed_plate/1", "decision": decision, "printer": printer, "copies": copies,
            "fault_read_as": "printer", "why": ["A's own reading points at the machine", f"The copies print on {printer}"],
            "summary": f"The copies print on {printer}.", **over}


def _frame() -> bytes:
    """A real camera-sized frame (noise, so it reads as a picture)."""
    import io
    import random

    from PIL import Image

    rng = random.Random(7)
    img = Image.new("RGB", (640, 480))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(640 * 480)])
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def _camera(printer, *, own: bool = False) -> None:
    """Give a stand-in printer a camera Kiln can read: by default one the
    person registered beside it (an external camera, ``camera_snapshot_url``);
    *own* for the printer's own, fitted at the factory."""
    type(printer).snapshot_source = PropertyMock(return_value="printer" if own else "user_supplied")
    if own:
        type(printer).camera_fitted_at_factory = PropertyMock(return_value=True)
    printer.get_snapshot.return_value = _frame()


class TestAFailedPlateOfAFleetRun:
    """The queue's own retry is the floor; the server's decision is acted on
    only when it comes, and a move only inside the person's yes.  A/B: on
    the tree before this change every case queues the copies on A again."""

    def _run(self, world, monkeypatch):
        """A kept, queued run: A's first plate printing; returns its job id and the scheduler."""
        from kiln import print_signoff
        from kiln.printers.base import JobResult

        plan_id = _plan(world)["plan_id"]
        # The person's yes for the run, as the go writes it onto every plate.
        monkeypatch.setattr(print_signoff, "record_for", lambda current: {"tool": "plan_multi_copy_split", "scope": None,
                                                                         "printer_name": None, "source": "elicited"})
        out = _go(world, plan_id, monkeypatch)
        assert out.get("success") is True, out
        job = next(j for j in world.q.list_jobs() if j.printer_name == "A").id
        world.q.mark_starting(job, printer_name="A")
        world.q.mark_printing(job)
        # The failed print stays on A's plate, as it would: the plate record
        # holds it, so a plate queued again there waits to be cleared rather
        # than start in the same tick.
        mark_occupied(world.printers["A"], JAR)
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._spawn = lambda work: work()
        sched._active_jobs[job] = "A"
        sched._seen_printing.add(job)
        a = world.printers["A"]

        def fails(faults=None):
            a.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE,
                                                    last_job_result=JobResult.FAILED, faults=faults)
            sched.tick()
        return plan_id, job, fails

    @staticmethod
    def _retried(world, job):
        """The plates queued again on the same printer after *job* failed
        (the same tick may already have started one on an idle printer, so
        they are known by their mark, never by their status)."""
        return [j for j in world.q.list_jobs() if j.id != job and (j.metadata.get("fill") or {}).get("failed_retries")
                and not (j.metadata.get("fill") or {}).get("moved_from")]

    @staticmethod
    def _moved(world):
        return [j for j in world.q.list_jobs() if (j.metadata.get("fill") or {}).get("moved_from")]

    def test_a_move_queues_the_copies_on_the_printer_named_and_says_why(self, world, monkeypatch):
        plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(B=(24, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails(faults=[{"code": "0700-8003", "kind": "hms", "screen_text": "The extruder is clogged."}])
        moved = self._moved(world)
        assert [(j.printer_name, j.metadata["fill"]["copies"], j.metadata["fill"]["moved_from"]) for j in moved] == [("B", 34, "A")]
        assert moved[0].metadata["split_plan_id"] == plan_id and moved[0].metadata["fill"]["failed_retries"] == 1
        assert self._retried(world, job) == []
        note = world.q.get_job(job).metadata["failed_plate"]
        assert note["decision"] == "move" and note["to"] == "B" and note["reasons"]
        # What was sent: the queue's own ask, with the printer's reading and the run's record.
        ask = world.sent[-1]
        assert ask["tool"] == "plan_fleet_fill" and ask["_asked_by_user"] is False and ask["copies"] == 34
        assert ask["failed"]["printer"] == "A" and ask["failed"]["tries"] == 1
        assert ask["failed"]["fault"]["codes"] == ["0700-8003"] and "clogged" in ask["failed"]["fault"]["lines"][0]
        assert ask["failed"]["run"]["A"] == {"done": 0, "failed": 1} and "B" in ask["failed"]["run"]
        assert "SERA" not in json.dumps(ask)

    def test_a_retry_queues_them_here_again(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(), "failed_plate": _verdict("retry", "A")}
        fails()
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in self._retried(world, job)] == [("A", 1)]
        assert world.q.get_job(job).metadata["failed_plate"]["decision"] == "retry"

    def test_a_stop_queues_nothing_and_carries_the_reason(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(), "failed_plate": _verdict(
            "stop", None, summary="Kiln did not queue these copies again: look at the part and the material.")}
        before = {j.id for j in world.q.list_jobs()}
        fails()
        assert {j.id for j in world.q.list_jobs()} == before
        assert "look at the part" in world.q.get_job(job).metadata["not_requeued"]

    def test_a_miss_keeps_the_same_printer_retry_and_says_so(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = ConnectionError("no route to host")
        fails()
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in self._retried(world, job)] == [("A", 1)]
        note = world.q.get_job(job).metadata["requeued_here"]
        assert "won't move any copies" in note and "The copies print again on A." in note
        assert "failed_plate" not in world.q.get_job(job).metadata

    def test_below_business_nothing_moves(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": False, "status": "error", "required_tier": "business",
                                 "error": "Fleet fill requires Kiln Business", "code": "TIER_REQUIRED"}
        fails()
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in self._retried(world, job)] == [("A", 1)]
        assert self._moved(world) == [] and "Kiln Business" in world.q.get_job(job).metadata["requeued_here"]

    def test_a_move_outside_the_yes_is_refused_in_words_and_the_copies_stay(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.q.update_metadata(job, {"preview_signoff": {"tool": "plan_multi_copy_split", "scope": ["A"]}})
        world.answer["value"] = {"success": True, "fill": _fill(B=(24, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails()
        assert world.sent[-1]["failed"]["allowed"] == ["A"]
        assert self._moved(world) == []
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in self._retried(world, job)] == [("A", 1)]
        note = world.q.get_job(job).metadata["requeued_here"]
        assert "would have moved these copies to B" in note and "does not name B" in note

    def test_at_the_cap_the_plate_stops_whatever_the_server_would_say(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.q.update_metadata(job, {"fill": {**world.q.get_job(job).metadata["fill"], "failed_retries": 2}})
        world.answer["value"] = {"success": True, "fill": _fill(B=(34, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        asked = len(world.sent)
        fails()
        # The cap is the queue's: nothing is asked, and nothing the server
        # could have said would move or retry the plate.
        assert len(world.sent) == asked
        assert self._retried(world, job) == [] and self._moved(world) == []
        assert "did not queue its copies" in world.q.get_job(job).metadata["not_requeued"]

    def test_one_printer_s_own_copies_never_ask(self, world, monkeypatch):
        """``multi_copy_print(fill=True)`` on one printer carries no run: the
        same-printer retry, with nothing sent."""
        from kiln.printers.base import JobResult

        job = world.srv._queue_fill_rest(world.part, 8, printer_name="A", printer_id=None, overrides=None,
                                         slicer_path=None, spacing_mm=10.0, signoff=None)["job_id"]
        world.q.mark_starting(job, printer_name="A")
        world.q.mark_printing(job)
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._spawn = lambda work: work()
        sched._active_jobs[job] = "A"
        sched._seen_printing.add(job)
        world.printers["A"].get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE,
                                                                   last_job_result=JobResult.FAILED)
        sched.tick()
        assert world.sent == []
        again = [j for j in world.q.list_jobs() if j.id != job]
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in again] == [("A", 1)]

    def test_the_run_status_counts_a_moved_plate(self, world, monkeypatch):
        plan_id, _job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(B=(24, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails()
        out = world.tools["split_plan_status"](plan_id)
        assert "1 of them moved to another printer" in out["message"], out["message"]


    def test_a_moved_plate_is_never_laid_beside_parts(self, world, monkeypatch):
        """B held the jar when the plan was kept.  A look says clear or
        occupied, never "exactly what was approved", so a move -- made with
        nobody at either printer -- carries no beside_plate at all: the
        copies wait for B's plate to be seen clear."""
        _plan_id, _job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(B=(34, 0, 0, "occupied")), "failed_plate": _verdict("move", "B")}
        fails()
        moved = self._moved(world)
        assert [j.printer_name for j in moved] == ["B"]
        assert "beside_plate" not in moved[0].metadata["fill"] and moved[0].metadata["fill"]["look_first"] is True

    def test_a_target_whose_plate_was_clear_in_the_plan_waits_for_a_cleared_plate(self, world, monkeypatch):
        """A's plate was clear when the plan was kept; a move back onto a
        printer with no approved occupant carries no beside_plate at all,
        whatever is on it now and whatever the answer's row says."""
        plan_id, job, fails = self._run(world, monkeypatch)
        from kiln import print_signoff
        monkeypatch.setattr(print_signoff, "record_for", lambda current: None)
        # Make B the failed printer's twin: queue the run's B plate as failed on B and move it to A.
        job_b = next(j for j in world.q.list_jobs() if j.printer_name == "B").id
        world.q.update_metadata(job_b, {"preview_signoff": {"tool": "plan_multi_copy_split", "scope": ["A", "B"]}})
        world.q.mark_starting(job_b, printer_name="B")
        world.q.mark_printing(job_b)
        sched = JobScheduler(world.q, world.reg, EventBus())
        sched._spawn = lambda work: work()
        sched._active_jobs[job_b] = "B"
        sched._seen_printing.add(job_b)
        from kiln.printers.base import JobResult
        world.printers["B"].get_state.return_value = PrinterState(connected=True, state=PrinterStatus.IDLE, last_job_result=JobResult.FAILED)
        world.answer["value"] = {"success": True, "fill": _fill(A=(6, 0, 0, "occupied")), "failed_plate": _verdict("move", "A", copies=6)}
        sched.tick()
        moved = [j for j in self._moved(world) if j.metadata["fill"]["moved_from"] == "B"]
        assert [j.printer_name for j in moved] == ["A"] and "beside_plate" not in moved[0].metadata["fill"]

    # --- the cameras, before a move (2026-10-26; each fails at 7fae6c3a) ---

    def _moved_to_b(self, world, monkeypatch, *, cameras=("A", "B"), own=()):
        """A plate fails on A and the answer moves its copies to B, B's own
        plate of the run already done, B's plate recorded clear by nobody's
        look (so only a look, or the person's word, can start the move)."""
        from kiln.plate_state import mark_clear

        for name in cameras:
            _camera(world.printers[name], own=name in own)
        plan_id, job, fails = self._run(world, monkeypatch)
        for other in world.q.list_jobs():
            if other.printer_name == "B" and other.id != job:
                world.q.cancel(other.id)
        mark_clear(world.printers["B"], "cli")
        world.answer["value"] = {"success": True, "fill": _fill(B=(34, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails(faults=[{"code": "0700-8003", "kind": "hms", "screen_text": "The extruder is clogged."}])
        moved = self._moved(world)
        assert [j.printer_name for j in moved] == ["B"], moved
        return job, moved[0].id

    @staticmethod
    def _tick_starts(world):
        sched = JobScheduler(world.q, world.reg, EventBus())
        started: list[str] = []
        sched._spawn = lambda work: None
        sched._start_fill_job = lambda j, p: started.append(j.id)
        sched.tick()
        return started

    def test_moved_copies_wait_until_the_new_plate_is_seen_through_its_camera(self, world, monkeypatch):
        """A move happens with nobody at either printer, and a clear RECORD
        is Kiln's belief, not a look: the copies start on B only once B's
        plate has been seen clear through its camera."""
        from kiln.plate_state import mark_from_camera
        from kiln.scheduler import LOOK_FIRST_CODE

        _job, moved = self._moved_to_b(world, monkeypatch)
        assert self._tick_starts(world) == []
        note = world.q.get_job(moved).metadata["waiting"]
        assert note["code"] == LOOK_FIRST_CODE
        assert "look at B's plate through its camera" in note["sentence"]
        assert mark_from_camera(world.printers["B"], seen="clear", judged_by="agent") == "clear"
        assert self._tick_starts(world) == [moved]

    def test_a_target_with_no_camera_waits_for_the_person_s_word(self, world, monkeypatch):
        from kiln.plate_state import SAID_DIRECTLY, mark_clear

        _job, moved = self._moved_to_b(world, monkeypatch, cameras=("A",))
        assert self._tick_starts(world) == []
        sentence = world.q.get_job(moved).metadata["waiting"]["sentence"]
        assert "B has no camera Kiln can read" in sentence and "once you say its plate is clear" in sentence
        mark_clear(world.printers["B"], SAID_DIRECTLY)
        assert self._tick_starts(world) == [moved]

    def test_the_failed_printer_s_picture_is_taken_kept_and_named_first(self, world, monkeypatch):
        """What failed shows on A's camera -- a part torn off, spaghetti, a
        print laid into the air -- and only eyes can tell which, so the wait
        asks for A's picture before anything else."""
        import os

        job, moved = self._moved_to_b(world, monkeypatch)
        picture = world.q.get_job(job).metadata["failed_picture"]
        assert os.path.isfile(picture)
        assert world.q.get_job(moved).metadata["fill"]["failed_picture"] == picture
        self._tick_starts(world)
        sentence = world.q.get_job(moved).metadata["waiting"]["sentence"]
        assert sentence.index(picture) < sentence.index("look at B's plate"), sentence
        assert "spaghetti means the part is the problem, so cancel these copies" in sentence

    def test_a_stop_names_the_picture_of_the_plate_that_failed(self, world, monkeypatch):
        _camera(world.printers["A"])
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.answer["value"] = {"success": True, "fill": _fill(), "failed_plate": _verdict(
            "stop", None, summary="Kiln did not queue these copies again: look at the part and the material.")}
        fails()
        meta = world.q.get_job(job).metadata
        assert meta["failed_picture"] in meta["not_requeued"]

    def test_a_printer_s_own_camera_and_an_external_one_are_both_looked_through(self, world, monkeypatch):
        """Some printers have a camera of their own; some people point a
        camera of their own at the plate.  Kiln looks through whichever the
        printer has: A's own camera, B's external one."""
        from kiln.plate_state import mark_from_camera

        job, moved = self._moved_to_b(world, monkeypatch, own=("A",))
        assert world.sent[-1]["failed"]["cameras"]["A"] is True and world.sent[-1]["failed"]["cameras"]["B"] is True
        assert world.printers["A"].get_snapshot.called and world.printers["B"].get_snapshot.called
        assert world.q.get_job(job).metadata["failed_picture"]
        mark_from_camera(world.printers["B"], seen="clear", judged_by="human")
        assert self._tick_starts(world) == [moved]

    def test_a_camera_that_is_not_answering_still_lets_the_person_say_so(self, world, monkeypatch):
        """B has a camera, but it gives no picture now (unplugged, asleep):
        the copies wait, and the person's own word that the plate is clear
        starts them, as for a printer with no camera."""
        from kiln.plate_state import SAID_DIRECTLY, mark_clear

        world.printers["B"].get_snapshot.side_effect = OSError("camera unplugged")
        _job, moved = self._moved_to_b(world, monkeypatch)
        world.printers["B"].get_snapshot.side_effect = OSError("camera unplugged")
        assert self._tick_starts(world) == []
        assert "or say yourself that it is clear" in world.q.get_job(moved).metadata["waiting"]["sentence"]
        mark_clear(world.printers["B"], SAID_DIRECTLY)
        assert self._tick_starts(world) == [moved]

    def test_a_failed_printer_whose_camera_gives_no_picture_is_told_to_the_server_as_none(self, world, monkeypatch):
        """A has a camera, but it gave no picture of the failed plate: the
        server is told there is no picture, so it never says Kiln took one."""
        world.printers["A"].get_snapshot.side_effect = OSError("camera asleep")
        job, _moved = self._moved_to_b(world, monkeypatch)
        assert world.sent[-1]["failed"]["cameras"]["A"] is False
        assert "failed_picture" not in world.q.get_job(job).metadata

    def test_with_no_camera_on_the_failed_printer_the_person_is_asked_to_look_there_first(self, world, monkeypatch):
        job, moved = self._moved_to_b(world, monkeypatch, cameras=("B",))
        assert "failed_picture" not in world.q.get_job(job).metadata
        self._tick_starts(world)
        sentence = world.q.get_job(moved).metadata["waiting"]["sentence"]
        assert "Kiln has no picture of A's plate, so look at it yourself first" in sentence

    def test_the_server_hears_only_which_printers_have_a_camera(self, world, monkeypatch):
        self._moved_to_b(world, monkeypatch, cameras=("A",))
        sent = world.sent[-1]["failed"]["cameras"]
        assert sent["A"] is True and sent["B"] is False
        assert all(isinstance(v, bool) for v in sent.values())
        text = json.dumps(world.sent[-1])
        assert "kiln_plate_" not in text and "image_b64" not in text and "/9j/" not in text, "no picture leaves"

    def test_a_printer_outside_the_run_is_refused_even_under_a_fleet_wide_yes(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.q.update_metadata(job, {"preview_signoff": {"tool": "plan_multi_copy_split", "scope": "fleet"}})
        world.answer["value"] = {"success": True, "fill": _fill(C=(34, 0, 0, "clear")), "failed_plate": _verdict("move", "C")}
        fails()
        assert self._moved(world) == []
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in self._retried(world, job)] == [("A", 1)]
        assert "C is not one of this run's printers" in world.q.get_job(job).metadata["requeued_here"]

    def test_a_job_with_no_yes_on_record_moves_nowhere(self, world, monkeypatch):
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.q.update_metadata(job, {"preview_signoff": None})
        world.answer["value"] = {"success": True, "fill": _fill(B=(34, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails()
        assert self._moved(world) == []
        assert "does not name B" in world.q.get_job(job).metadata["requeued_here"]

    def test_the_count_of_failed_plates_travels_with_a_move(self, world, monkeypatch):
        """Failed once on A and moved to B with the count at one; when it
        fails on B the queue asks with tries=2, and a second move carries
        two, so the third failure stops wherever it happens."""
        _plan_id, job, fails = self._run(world, monkeypatch)
        world.q.update_metadata(job, {"fill": {**world.q.get_job(job).metadata["fill"], "failed_retries": 1}})
        world.answer["value"] = {"success": True, "fill": _fill(B=(34, 0, 0, "clear")), "failed_plate": _verdict("move", "B")}
        fails()
        assert world.sent[-1]["failed"]["tries"] == 2
        moved = self._moved(world)
        assert [(j.printer_name, j.metadata["fill"]["failed_retries"]) for j in moved] == [("B", 2)]

class TestWhichPrintersAYesCovers:
    def test_the_rule_the_queue_checks_with_asked_the_other_way_round(self):
        from kiln.print_signoff import covered_printers, record_refusal

        for record, covered in (
            ({"scope": "fleet"}, None),
            ({"scope": ["A", "B"], "printer_name": "A"}, ["A", "B"]),
            ({"printer_name": "A"}, ["A"]),
            ({"printer_name": None}, None),
            (None, None),
        ):
            assert covered_printers(record) == covered, record
            for name in ("A", "B", "C"):
                assert (record_refusal(record, name) is None) == (covered is None or name in covered), (record, name)

    def test_naming_a_printer_outright_is_stricter_than_not_refusing_it(self):
        from kiln.print_signoff import names_printer

        assert names_printer({"scope": "fleet"}, "C") is True
        assert names_printer({"scope": ["A", "B"]}, "B") is True and names_printer({"scope": ["A", "B"]}, "C") is False
        assert names_printer({"printer_name": "A"}, "A") is True and names_printer({"printer_name": "A"}, "B") is False
        assert names_printer({"printer_name": None}, "A") is False and names_printer(None, "A") is False
