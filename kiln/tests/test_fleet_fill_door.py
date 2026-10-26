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


def _fill(**rows) -> dict:
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
    printers = {n: _printer(n) for n in ("A", "B", "C")}
    for n, a in printers.items():
        reg.register(n, a)
    mark_clear(printers["A"], "human")
    mark_occupied(printers["B"], JAR)
    part = _cube(tmp_path / "clip.stl")
    return SimpleNamespace(srv=srv, q=q, reg=reg, tools=_MCP.tools, printers=printers, part=part, sent=sent,
                           answer=answer, tier=tier)


def _plan(world, copies=40, **rows):
    world.answer["value"] = {"success": True, "fill": _fill(**(rows or {"A": (24, 10, 1, "clear"), "B": (6, 0, 0, "occupied")}))}
    return world.tools["plan_multi_copy_split"](world.part, copies, material="PLA")


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
        assert st["message"] == "40 copies: 0 plates printed, 0 printing, 3 waiting; 50 copies still to come."
        out = world.tools["cancel_split_plan"](plan["plan_id"])
        assert out["cancelled"] == 3
        assert world.q.get_job(later.id).status == JobStatus.CANCELLED

    def test_cancelling_a_spread_plan_cancels_its_jobs(self, world):
        """A/B: with the old ``from kiln.queue import get_queue`` back, every
        part reports "Failed to cancel" and nothing is cancelled -- "assert (0 == 2)"."""
        from kiln.job_splitter import SplitJob, SplitPlan, cancel_split_plan, submit_split_plan

        world.srv._preview_gate_error = lambda *a, **k: None
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
