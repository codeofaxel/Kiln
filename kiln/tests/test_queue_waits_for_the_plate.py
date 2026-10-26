"""The queue waits for the plate, and for its turn.

Until this change the scheduler started a queued job on any idle printer
without reading the plate record, so the next job's start sequence drove
the head across the finished part the last print left there.  Every door
a person starts a print from asked the record first
(``plate_state.start_refusal``); the queue, which starts prints with nobody
there, did not.  Now it asks the same record before every start and holds
the job, in its place and with no retry spent, until the plate is cleared.

Beside it, one printer at a time below Kiln Business: a start the start
gate turns away because another printer is printing used to be read as a
failed start, spending retries until the job FAILED.  It now waits its
turn.

The scheduler is driven through ``tick()`` with fake printers whose plate
record is the real one (``mark_occupied`` / ``mark_clear`` against a
machine with a serial, in a per-test ``KILN_HOME``).

A/B, recorded where it was run: each test that pins a fix names the
mutation of the source it was run against and the assertion that failed.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln import plate_state
from kiln.events import EventBus, EventType
from kiln.plate_state import (
    PLATE_CHANGED_CODE,
    START_NOT_YET_CODE,
    PlateJob,
    fingerprint,
    mark_clear,
    mark_occupied,
    read,
)
from kiln.printers.base import (
    CodedPrintResult,
    JobProgress,
    PrinterCapabilities,
    PrinterEngagementError,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import TURN_RECHECK_S, TURN_UPGRADE, JobScheduler, describe_wait

PART = PlateJob(file="vase_v3.gcode", max_z_mm=42.0)
TURN_REFUSAL = CodedPrintResult(
    success=False,
    message="Kiln runs one printer at a time on this plan, and B is already printing.",
    refused_before_send=True,
    code="TIER_CONCURRENT_PRINT_LIMIT",
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _printer(name: str, serial: str, state: PrinterStatus = PrinterStatus.IDLE) -> MagicMock:
    """A fake printer with a serial, so the plate record can be kept for it."""
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value=name)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.serial = serial
    adapter.snapshot_source = None  # no camera: the sentences stay camera-free
    adapter.get_state.return_value = PrinterState(connected=True, state=state)
    adapter.get_job.return_value = JobProgress()
    adapter.start_print.return_value = PrintResult(success=True, message="started")
    return adapter


def _set_state(adapter: MagicMock, state: PrinterStatus) -> None:
    adapter.get_state.return_value = PrinterState(connected=True, state=state)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    import kiln.emergency as emergency

    monkeypatch.setattr(emergency, "_coordinator", None)


@pytest.fixture()
def world():
    """A queue, a registry, a bus and a scheduler on a clock the test moves.

    ``max_retries=2`` on purpose: a start wrongly read as a failure spends a
    retry and stays visible, instead of failing outright and hiding which
    path it took.
    """
    queue, registry, bus, clock = PrintQueue(), PrinterRegistry(), EventBus(), _Clock()
    sched = JobScheduler(queue, registry, bus, poll_interval=0.1, max_retries=2)
    sched._clock = clock

    def add(name: str, serial: str, state: PrinterStatus = PrinterStatus.IDLE) -> MagicMock:
        adapter = _printer(name, serial, state)
        registry.register(name, adapter)
        return adapter

    return SimpleNamespace(queue=queue, registry=registry, bus=bus, clock=clock, sched=sched, add=add)


def _waiting_events(bus: EventBus) -> list:
    """Every JOB_WAITING published, oldest first."""
    return list(reversed(bus.recent_events(EventType.JOB_WAITING, limit=1000)))


def _note(queue: PrintQueue, job_id: str) -> dict | None:
    return (queue.get_job(job_id).metadata or {}).get("waiting")


def _quiet_file(tmp_path, adapter, name: str = "beside_quiet.gcode") -> str:
    """A G-code file carrying Kiln's own quiet-start header, written by the
    real header writer, for *adapter*'s plate as the record stands now."""
    from kiln.printers.bambu_3mf import _quiet_start_header
    from kiln.printers.print_gate import same_bed_machine_id

    plan = {
        "planned_for_machine": same_bed_machine_id(adapter),
        "plate_fingerprint": fingerprint(read(adapter)),
        "clear_z_mm": 47.0, "lift_floor_mm": 47.0,
        "flags": {"bed_leveling": False, "timelapse": False},
    }
    path = tmp_path / name
    path.write_text(_quiet_start_header(plan) + "G1 X10 Y10\n")
    return str(path)


# ---------------------------------------------------------------------------
# 1. The safety case: a finished part holds the next job
# ---------------------------------------------------------------------------


class TestTheFinishedPartHoldsTheLine:
    def test_an_occupied_plate_holds_the_job_until_it_is_cleared(self, world):
        """The incident: an idle printer with the last print still on it, and
        one job in line.  Nothing may start; the job keeps its place, spends
        no retry, and says why once.  When a person clears the plate, the
        next pass starts it and the note goes.

        A/B: run with ``_startable_job`` returning ``candidates[0]``
        unconditionally (what ``next_job`` did before), this fails at
        ``a.start_print.assert_not_called()`` -- "Expected 'start_print' to
        not have been called. Called 1 times."
        """
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode", submitted_by="test")

        result = world.sched.tick()

        a.start_print.assert_not_called()
        assert result["dispatched"] == [] and result["failed"] == []
        assert world.queue.get_job(job).status == JobStatus.QUEUED
        note = _note(world.queue, job)
        assert note["kind"] == "plate" and note["code"] == START_NOT_YET_CODE and note["printers"] == ["A"]
        assert "vase v3" in note["sentence"] and "kiln plate clear" in note["sentence"]
        assert note["since"]
        assert world.sched._retry_counts.get(job, 0) == 0, "a wait is not a failed start"
        assert job not in world.sched._retry_not_before
        events = _waiting_events(world.bus)
        assert len(events) == 1
        assert events[0].data["kind"] == "plate" and events[0].data["printer_name"] == "A"
        assert events[0].data["waiting_jobs"] == [job]

        mark_clear(a, "human")
        result = world.sched.tick()

        a.start_print.assert_called_once_with("bracket.gcode")
        assert [d["job_id"] for d in result["dispatched"]] == [job]
        assert world.queue.get_job(job).status == JobStatus.PRINTING
        assert _note(world.queue, job) is None, "the wait is over and the note with it"

    def test_ten_passes_announce_once_and_a_new_part_announces_again(self, world, monkeypatch):
        """The wait is announced when it begins, not every few seconds, and
        the note is written once, not every pass.  A second print's part (a
        new record, a new moment) is a new wait and is announced."""
        stamp = {"now": "2026-10-25T09:00:00-07:00"}
        monkeypatch.setattr(plate_state, "_now_iso", lambda: stamp["now"])
        writes: list[str] = []
        real_write = world.queue._update_metadata_db
        monkeypatch.setattr(world.queue, "_update_metadata_db", lambda j: writes.append(j.id) or real_write(j))
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode")

        for _ in range(10):
            world.clock.advance(5)
            world.sched.tick()

        a.start_print.assert_not_called()
        assert len(_waiting_events(world.bus)) == 1
        assert writes == [job], "one note, written once"

        stamp["now"] = "2026-10-25T11:30:00-07:00"
        mark_occupied(a, PlateJob(file="lid.gcode", max_z_mm=8.0))
        for _ in range(3):
            world.clock.advance(5)
            world.sched.tick()

        events = _waiting_events(world.bus)
        assert len(events) == 2
        assert "lid" in events[1].data["message"] and "vase" not in events[1].data["message"]
        a.start_print.assert_not_called()
        assert "lid" in _note(world.queue, job)["sentence"]

    def test_a_quiet_start_for_this_plate_starts_while_the_job_ahead_waits(self, world, tmp_path):
        """A file sliced to start beside exactly what is on the plate starts
        as it would from any door; the ordinary job ahead of it keeps its
        place.  The contract is real: Kiln's own header writer, read back by
        ``start_hold`` through the file's path (nothing monkeypatched)."""
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        quiet = _quiet_file(tmp_path, a)
        assert plate_state.start_hold(a, file_name=quiet) is None, "the contract names this plate"
        ordinary = world.queue.submit("bracket.gcode", printer_name="A")
        beside = world.queue.submit(quiet, printer_name="A")

        world.sched.tick()

        a.start_print.assert_called_once_with(quiet)
        assert world.queue.get_job(beside).status == JobStatus.PRINTING
        assert world.queue.get_job(ordinary).status == JobStatus.QUEUED
        assert _note(world.queue, ordinary)["kind"] == "plate"
        assert _waiting_events(world.bus)[0].data["waiting_jobs"] == [ordinary]

    def test_a_quiet_start_for_a_plate_that_changed_waits_and_says_so(self, world, tmp_path):
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        quiet = _quiet_file(tmp_path, a)
        mark_occupied(a, PlateJob(file="lid.gcode", max_z_mm=8.0), keep_previous=True)  # the plate moved on
        job = world.queue.submit(quiet, printer_name="A")

        world.sched.tick()

        a.start_print.assert_not_called()
        note = _note(world.queue, job)
        assert note["kind"] == "plate" and note["code"] == PLATE_CHANGED_CODE
        assert "Slice it again" in note["sentence"]


# ---------------------------------------------------------------------------
# 2. More than one printer
# ---------------------------------------------------------------------------


class TestAFleet:
    def test_a_job_any_printer_may_take_starts_on_the_clear_one_and_says_nothing(self, world):
        a, b = world.add("A", "SER-A"), world.add("B", "SER-B")
        mark_occupied(a, PART)
        mark_clear(b, "human")
        job = world.queue.submit("bracket.gcode")

        world.sched.tick()

        a.start_print.assert_not_called()
        b.start_print.assert_called_once_with("bracket.gcode")
        assert world.sched.active_jobs == {job: "B"}
        assert _note(world.queue, job) is None, "a job that started is not waiting"
        assert _waiting_events(world.bus) == [], "A held nothing that did not start elsewhere"

    def test_a_job_for_the_occupied_printer_waits_and_the_clear_one_leaves_it(self, world):
        a, b = world.add("A", "SER-A"), world.add("B", "SER-B")
        mark_occupied(a, PART)
        mark_clear(b, "human")
        job = world.queue.submit("bracket.gcode", printer_name="A")

        world.sched.tick()

        a.start_print.assert_not_called()
        b.start_print.assert_not_called()
        note = _note(world.queue, job)
        assert note["kind"] == "plate" and note["printers"] == ["A"]

    def test_every_printer_occupied_is_one_note_naming_them_all(self, world):
        a, b = world.add("A", "SER-A"), world.add("B", "SER-B")
        mark_occupied(a, PART)
        mark_occupied(b, PlateJob(file="lid.gcode"))
        job = world.queue.submit("bracket.gcode")

        world.sched.tick()

        a.start_print.assert_not_called()
        b.start_print.assert_not_called()
        note = _note(world.queue, job)
        assert note["kind"] == "plate" and note["printers"] == ["A", "B"]
        assert "A and B" in note["sentence"] and "a printer with a clear plate" in note["sentence"]
        assert sorted(e.data["printer_name"] for e in _waiting_events(world.bus)) == ["A", "B"]


# ---------------------------------------------------------------------------
# 3. One printer at a time: the turn
# ---------------------------------------------------------------------------


class TestTheTurn:
    """A/B for every test here: run with ``_TURN_CODES = frozenset()`` (both
    turn branches dead, the pre-change path), the coded refusal goes through
    ``resolve_print_start`` as a failed start and ``_requeue_or_fail`` spends
    a retry -- "assert 1 == 0" at the ``_retry_counts`` line."""

    def _turned_away(self, world, refusal):
        a = world.add("A", "SER-A")
        b = world.add("B", "SER-B", PrinterStatus.PRINTING)
        if isinstance(refusal, Exception):
            a.start_print.side_effect = refusal
        else:
            a.start_print.return_value = refusal
        job = world.queue.submit("bracket.gcode", printer_name="A")
        result = world.sched.tick()
        return a, b, job, result

    def _assert_waits_its_turn(self, world, job, result):
        assert result["failed"] == [] and result["dispatched"] == []
        assert world.queue.get_job(job).status == JobStatus.QUEUED
        assert world.sched._retry_counts.get(job, 0) == 0
        assert job not in world.sched._retry_not_before
        note = _note(world.queue, job)
        assert note["kind"] == "turn" and note["waiting_for"] == ["B"] and note["upgrade"] == TURN_UPGRADE
        assert "B is printing" in note["sentence"] and "keeps its place" in note["sentence"]
        events = _waiting_events(world.bus)
        assert len(events) == 1 and events[0].data["kind"] == "turn"

    def test_a_coded_refusal_keeps_the_job_and_asks_again_when_the_other_printer_is_idle(self, world):
        a, b, job, result = self._turned_away(world, TURN_REFUSAL)
        assert a.start_print.call_count == 1
        self._assert_waits_its_turn(world, job, result)

        for _ in range(5):
            world.clock.advance(30)
            world.sched.tick()
        assert a.start_print.call_count == 1, "nothing is asked while the other printer prints"
        assert len(_waiting_events(world.bus)) == 1

        _set_state(b, PrinterStatus.IDLE)
        a.start_print.return_value = PrintResult(success=True, message="started")
        world.sched.tick()
        assert a.start_print.call_count == 2
        assert world.queue.get_job(job).status == JobStatus.PRINTING
        assert _note(world.queue, job) is None

    def test_after_the_recheck_interval_it_asks_again_even_while_the_other_prints(self, world):
        a, _b, job, _ = self._turned_away(world, TURN_REFUSAL)
        world.clock.advance(TURN_RECHECK_S - 1)
        world.sched.tick()
        assert a.start_print.call_count == 1
        world.clock.advance(1)
        world.sched.tick()
        assert a.start_print.call_count == 2, "asked again once the interval passed"
        assert world.queue.get_job(job).status == JobStatus.QUEUED
        assert world.sched._retry_counts.get(job, 0) == 0

    def test_the_engagement_rule_is_a_turn_too(self, world):
        refusal = PrinterEngagementError({"code": "TIER_SINGLE_PRINTER_LIMIT", "reason": "Kiln is working with B."})
        a, _b, job, result = self._turned_away(world, refusal)
        self._assert_waits_its_turn(world, job, result)
        world.clock.advance(30)
        world.sched.tick()
        assert a.start_print.call_count == 1

    def test_any_other_engagement_refusal_still_spends_a_retry(self, world):
        """Only the two turn codes wait; anything else is the old path."""
        refusal = PrinterEngagementError({"code": "SOMETHING_ELSE", "reason": "no"})
        _a, _b, job, result = self._turned_away(world, refusal)
        assert world.sched._retry_counts.get(job) == 1
        assert job in world.sched._retry_not_before
        assert world.sched._turn_wait is None
        assert _waiting_events(world.bus) == []
        assert (_note(world.queue, job) or {}).get("kind") != "turn"

    def test_a_turn_wait_still_says_when_a_printers_plate_holds_its_job(self, world):
        """While another printer's print holds the turn, the plates are still
        read: a job pinned to an idle printer with a part on it says
        "plate" -- the thing a person can act on -- not "turn", and that
        printer is never asked to start.

        A/B: with ``if turn_held: available = []`` restored after
        ``turn_held`` is computed (the earlier version), job_a's note reads
        "turn" -- "AssertionError: assert 'turn' == 'plate'"."""
        a = world.add("A", "SER-A")
        b = world.add("B", "SER-B")
        world.add("C", "SER-C", PrinterStatus.PRINTING)
        b.start_print.return_value = TURN_REFUSAL
        job_b = world.queue.submit("lid.gcode", printer_name="B")
        world.sched.tick()
        assert _note(world.queue, job_b)["kind"] == "turn" and world.sched._turn_wait is not None

        mark_occupied(a, PART)
        job_a = world.queue.submit("bracket.gcode", printer_name="A")
        world.clock.advance(10)
        world.sched.tick()

        assert world.sched._turn_wait is not None, "the turn still holds"
        assert _note(world.queue, job_a)["kind"] == "plate"
        assert _note(world.queue, job_a)["printers"] == ["A"]
        assert _note(world.queue, job_b)["kind"] == "turn"
        a.start_print.assert_not_called()
        assert b.start_print.call_count == 1
        kinds = [(e.data["kind"], e.data["printer_name"]) for e in _waiting_events(world.bus)]
        assert kinds == [("turn", "B"), ("plate", "A")]

    def test_a_job_only_the_turn_holds_is_not_sent_to_another_printers_plate(self, world):
        """A clear idle printer (A) would take the job but for the turn (B is
        printing).  A third idle printer (C) holds a part.  Clearing C cannot
        start the job, so its note should stay "turn" (and the summary's
        upgrade line with it).  Measured: the note becomes "plate" for C, and
        clearing C leaves the job queued, unstarted."""
        a = world.add("A", "SER-A")
        world.add("B", "SER-B", PrinterStatus.PRINTING)
        c = world.add("C", "SER-C")
        a.start_print.return_value = TURN_REFUSAL
        job = world.queue.submit("bracket.gcode")
        world.sched.tick()
        assert _note(world.queue, job)["kind"] == "turn"

        mark_occupied(c, PART)
        world.clock.advance(10)
        world.sched.tick()

        assert _note(world.queue, job)["kind"] == "turn"


# ---------------------------------------------------------------------------
# 4. The queue's own store
# ---------------------------------------------------------------------------


class TestTheQueueStore:
    NOTE = {
        "kind": "plate", "code": START_NOT_YET_CODE, "printers": ["A"],
        "sentence": "bracket is next on A.", "since": "2026-10-25T09:00:00+00:00",
    }

    def test_a_note_survives_a_new_queue_on_the_same_store_and_clears(self, tmp_path):
        db = str(tmp_path / "queue.db")
        q = PrintQueue(db_path=db)
        job = q.submit("bracket.gcode", printer_name="A", metadata={"design_id": "d1"})
        q.note_waiting(job, self.NOTE)

        again = PrintQueue(db_path=db)
        assert again.get_job(job).metadata == {"design_id": "d1", "waiting": self.NOTE}

        again.note_waiting(job, None)
        third = PrintQueue(db_path=db)
        assert third.get_job(job).metadata == {"design_id": "d1"}

    def test_the_same_note_twice_is_one_write(self, monkeypatch):
        q = PrintQueue()
        job = q.submit("bracket.gcode")
        writes: list[str] = []
        real_write = q._update_metadata_db
        monkeypatch.setattr(q, "_update_metadata_db", lambda j: writes.append(j.id) or real_write(j))

        q.note_waiting(job, self.NOTE)
        q.note_waiting(job, dict(self.NOTE))
        assert writes == [job]
        q.note_waiting(job, None)
        q.note_waiting(job, None)
        assert writes == [job, job]
        q.note_waiting("no-such-job", self.NOTE)  # never raises
        assert writes == [job, job]

    def test_requeue_moves_only_a_starting_job(self, tmp_path):
        db = str(tmp_path / "queue.db")
        q = PrintQueue(db_path=db)
        queued = q.submit("a.gcode")
        printing = q.submit("b.gcode")
        q.mark_starting(printing)
        q.mark_printing(printing)
        started_at = q.get_job(printing).started_at

        assert q.requeue(queued).status == JobStatus.QUEUED
        assert q.requeue(printing).status == JobStatus.PRINTING, "a printing job is not put back in line"
        assert q.get_job(printing).started_at == started_at
        cancelled = q.submit("c.gcode")
        q.cancel(cancelled)
        assert q.requeue(cancelled).status == JobStatus.CANCELLED

        starting = q.submit("d.gcode")
        q.mark_starting(starting)
        back = q.requeue(starting)
        assert back.status == JobStatus.QUEUED and back.started_at is None and back.error is None
        row = sqlite3.connect(db).execute("SELECT status FROM jobs WHERE id=?", (starting,)).fetchone()
        assert row == ("queued",)

    def test_queued_for_is_the_order_the_queue_runs(self):
        q = PrintQueue()
        low = q.submit("low.gcode")
        pinned_b = q.submit("b.gcode", printer_name="B")
        high = q.submit("high.gcode", printer_name="A", priority=5)
        assert [j.id for j in q.queued_for("A")] == [high, low]
        assert [j.id for j in q.queued_for(None)] == [high, low, pinned_b]
        assert q.next_job("A").id == high and q.next_job("B").id == low


# ---------------------------------------------------------------------------
# 5. The start gate's code rides the result
# ---------------------------------------------------------------------------


class TestTheStartTemplate:
    def _printer(self):
        from .test_a_refused_start_is_not_accepted import _Printer

        return _Printer("SERIAL-A")

    def test_a_coded_gate_refusal_carries_its_code(self, monkeypatch):
        printer = self._printer()
        monkeypatch.setattr(
            "kiln.printers.print_gate.run_adapter_gate",
            lambda *a, **k: {
                "blocked": True, "reason": "Kiln runs one printer at a time.", "override_hint": "Wait for B.",
                "code": "TIER_CONCURRENT_PRINT_LIMIT",
            },
        )
        result = printer.start_print("part.gcode")
        assert isinstance(result, CodedPrintResult) and result.code == "TIER_CONCURRENT_PRINT_LIMIT"
        assert result.success is False and result.refused_before_send is True
        assert result.message == "Kiln runs one printer at a time. Wait for B."
        assert printer.sent == []

    def test_an_uncoded_refusal_keeps_the_keys_it_always_had(self, monkeypatch):
        printer = self._printer()
        monkeypatch.setattr(
            "kiln.printers.print_gate.run_adapter_gate",
            lambda *a, **k: {"blocked": True, "reason": "too hot for this hotend"},
        )
        result = printer.start_print("part.gcode")
        assert type(result) is PrintResult
        assert result.to_dict() == {
            "success": False, "message": "too hot for this hotend", "job_id": None, "refused_before_send": True,
        }

    def test_the_real_fleet_gate_puts_a_queued_job_in_line_for_its_turn(self, monkeypatch, tmp_path):
        """End to end, nothing stubbed between the gate and the queue: two
        real adapter subclasses, the second printing, the real
        ``_concurrent_fleet_verdict`` on a one-printer plan.  The job waits
        its turn, nothing reaches the printer, and when the other printer is
        done the job starts."""
        import sys
        import types

        import kiln.registry as registry_mod

        from .test_a_refused_start_is_not_accepted import _Printer

        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        lic = sys.modules.get("kiln.licensing")
        if lic is None:
            lic = types.ModuleType("kiln.licensing")
            monkeypatch.setitem(sys.modules, "kiln.licensing", lic)
        monkeypatch.setattr(lic, "get_tier", lambda: "free", raising=False)
        monkeypatch.setattr(lic, "max_printers_for_tier", lambda _t: 1, raising=False)

        queue, registry, bus = PrintQueue(), PrinterRegistry(), EventBus()
        monkeypatch.setattr(registry_mod, "get_registry", lambda: registry)
        a, b = _Printer("SER-A"), _Printer("SER-B", PrinterStatus.PRINTING)
        registry.register("A", a)
        registry.register("B", b)
        sched = JobScheduler(queue, registry, bus, poll_interval=0.1, max_retries=2)
        sched._clock = _Clock()
        job = queue.submit("part.gcode", printer_name="A")

        sched.tick()

        assert a.sent == [], "nothing reached the printer"
        assert queue.get_job(job).status == JobStatus.QUEUED
        assert _note(queue, job)["kind"] == "turn" and _note(queue, job)["code"] == "TIER_CONCURRENT_PRINT_LIMIT"
        assert sched._retry_counts.get(job, 0) == 0

        b.status = PrinterStatus.IDLE
        sched.tick()
        assert a.sent == ["part.gcode"]
        assert queue.get_job(job).status == JobStatus.PRINTING


class TestStartHoldIsStartRefusalsDecision:
    """``start_refusal``'s outputs themselves (the look fields, the changed-
    plate message, clear and resume) are pinned by test_plate_camera.py and
    test_quiet_start.py, run beside this file; here only that the new
    look-free decision agrees with it."""

    def test_the_two_agree_in_every_state(self, tmp_path):
        m = SimpleNamespace(name="bambu", serial="01P00A000000001", _printer_model="bambu_a1", snapshot_source=None)
        assert plate_state.start_hold(m) is None and plate_state.start_refusal(m) is None  # no record
        mark_occupied(m, PART)
        code, state = plate_state.start_hold(m)
        assert code == START_NOT_YET_CODE and state == read(m)
        assert plate_state.start_refusal(m)["error"]["code"] == code
        assert plate_state.start_hold(m, resume=True) is None and plate_state.start_refusal(m, resume=True) is None
        quiet = _quiet_file(tmp_path, m)
        assert plate_state.start_hold(m, file_name=quiet) is None
        mark_occupied(m, PlateJob(file="lid.gcode"), keep_previous=True)
        assert plate_state.start_hold(m, file_name=quiet)[0] == PLATE_CHANGED_CODE
        assert plate_state.start_refusal(m, file_name=quiet)["error"]["code"] == PLATE_CHANGED_CODE
        mark_clear(m, "human")
        assert plate_state.start_hold(m) is None and plate_state.start_refusal(m) is None


# ---------------------------------------------------------------------------
# 6. Readers: what a person is told
# ---------------------------------------------------------------------------


class TestDescribeWait:
    def test_each_reason_and_the_place_in_line(self):
        q = PrintQueue()
        first = q.submit("first.gcode")
        second = q.submit("second.gcode", printer_name="A")
        third = q.submit("third.gcode")
        on_a = q.submit("vase_v3.gcode", printer_name="A")
        q.mark_starting(on_a)
        q.mark_printing(on_a)
        job = q.get_job

        assert describe_wait(job(on_a), q) is None, "a printing job is not waiting"
        assert describe_wait(job(first), q) == {"kind": "in_line", "ahead": 0, "sentence": "first is next in line."}
        assert describe_wait(job(second), q)["sentence"] == "second is in line behind 1 other job."
        assert describe_wait(job(third), q)["ahead"] == 2
        assert describe_wait(job(third), q)["sentence"] == "third is in line behind 2 other jobs."

        printing = describe_wait(job(second), q, running={"A": on_a}, idle=frozenset())
        assert printing["kind"] == "printing" and printing["printers"] == ["A"]
        assert printing["sentence"] == (
            "A is printing vase v3. second is in line behind 1 other job and starts when it finishes and the plate "
            "is clear."
        )
        busy = describe_wait(job(second), q, running={}, idle=frozenset({"B"}))
        assert busy["kind"] == "busy" and busy["sentence"].startswith("A is busy or not answering right now.")
        everyone = describe_wait(job(first), q, running={}, idle=frozenset())
        assert everyone["kind"] == "busy" and everyone["sentence"].startswith("Every printer is busy")

        retry = describe_wait(job(first), q, retry_at=time.time() + 600)
        assert retry["kind"] == "retry" and "tries it again at" in retry["sentence"]
        assert describe_wait(job(first), q, retry_at=time.time() - 1)["kind"] == "in_line", "a retry already due"

    def test_a_written_note_comes_first_and_carries_its_place(self):
        q = PrintQueue()
        q.submit("ahead.gcode")
        job = q.submit("bracket.gcode", printer_name="A")
        note = {"kind": "plate", "code": START_NOT_YET_CODE, "printers": ["A"], "sentence": "Clear A.", "since": "x"}
        q.note_waiting(job, note)
        said = describe_wait(q.get_job(job), q, running={}, idle=frozenset(), retry_at=time.time() + 600)
        assert said == {**note, "ahead": 1}


@pytest.fixture()
def server(world, monkeypatch):
    """``kiln.server`` pointed at this world's queue, registry and scheduler."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_get_queue", lambda: world.queue)
    monkeypatch.setattr(srv, "_get_registry", lambda: world.registry)
    monkeypatch.setattr(srv, "_scheduler", world.sched)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    return srv


def _summary_world(world):
    """Two jobs waiting their turn on A (B is printing) and one waiting for
    C's plate."""
    a = world.add("A", "SER-A")
    world.add("B", "SER-B", PrinterStatus.PRINTING)
    c = world.add("C", "SER-C")
    a.start_print.return_value = TURN_REFUSAL
    mark_occupied(c, PART)
    first = world.queue.submit("bracket.gcode", printer_name="A")
    second = world.queue.submit("hook.gcode", printer_name="A")
    held = world.queue.submit("lid.gcode", printer_name="C")
    world.sched.tick()
    world.clock.advance(10)
    world.sched.tick()
    return first, second, held


class TestTheToolsSayIt:
    def test_queue_summary_lists_each_wait_the_plates_and_the_plan_once(self, world, server):
        from kiln.plugins import queue_tools

        first, second, held = _summary_world(world)
        out = queue_tools.queue_summary()

        assert out["success"] is True, out
        assert [(w["job_id"], w["kind"]) for w in out["waiting"]] == [
            (first, "turn"), (second, "turn"), (held, "plate"),
        ]
        assert out["plates_to_clear"] == ["C"]
        assert out["upgrade"] == TURN_UPGRADE
        assert all("upgrade" not in w for w in out["waiting"]), "the plan is said once, not per job"
        assert out["waiting"][1]["ahead"] == 1

    def test_job_status_carries_the_wait(self, world, server):
        from kiln.plugins import queue_tools

        _first, _second, held = _summary_world(world)
        out = queue_tools.job_status(held)
        assert out["waiting"]["kind"] == "plate" and out["waiting"]["printers"] == ["C"]
        assert out["waiting"]["sentence"] == out["job"]["metadata"]["waiting"]["sentence"]

    def test_next_up_after_clear_names_only_a_job_held_by_this_plate(self, world, server, tmp_path):
        from kiln.plugins.queue_tools import next_up_after_clear

        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode", printer_name="A")
        world.sched.tick()
        up = next_up_after_clear("A")
        assert up["job_id"] == job and up["file_name"] == "bracket.gcode"
        assert "Next on A: bracket" in up["sentence"]
        assert next_up_after_clear("B") is None and next_up_after_clear(None) is None

        # A file planned beside a part that has since changed will not start
        # on an emptied plate, so it is never named as next.
        world.queue.cancel(job)
        quiet = _quiet_file(tmp_path, a)
        mark_occupied(a, PlateJob(file="lid.gcode"), keep_previous=True)
        changed = world.queue.submit(quiet, printer_name="A")
        world.sched.tick()
        assert _note(world.queue, changed)["code"] == PLATE_CHANGED_CODE
        assert next_up_after_clear("A") is None

    def test_clearing_the_plate_says_what_starts_next_and_the_next_pass_starts_it(self, world, server, monkeypatch):
        from kiln.plugins.homing_tools import run_plate

        a = world.add("A", "SER-A")
        monkeypatch.setattr(server, "_resolve_control_target", lambda name=None: (a, "A"))
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode", printer_name="A")
        world.sched.tick()

        out = run_plate(printer_name="A", action="clear")
        assert out["success"] is True and out["plate"]["status"] == "clear"
        assert out["next_up"]["job_id"] == job
        assert "next_up" not in run_plate(printer_name="A", action="status")

        world.sched.tick()
        a.start_print.assert_called_once_with("bracket.gcode")

    def _tools(self, monkeypatch, server, adapter):
        from kiln.plugins import homing_tools

        class _FakeMCP:
            def __init__(self):
                self.tools = {}

            def tool(self, *a, **k):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn
                return deco

        monkeypatch.setattr(server, "_resolve_control_target", lambda name=None: (adapter, "A"))
        mcp = _FakeMCP()
        homing_tools._HomingToolsPlugin().register(mcp)
        return mcp.tools

    def test_a_camera_look_that_sees_it_clear_says_what_starts_next(self, world, server, monkeypatch):
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode", printer_name="A")
        world.sched.tick()
        out = self._tools(monkeypatch, server, a)["look_at_plate"](seen="clear")
        assert out["success"] is True, out
        assert out["next_up"]["job_id"] == job

    def test_the_persons_word_tells_the_agent_not_to_start_it_again(self, world, server, monkeypatch):
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        job = world.queue.submit("bracket.gcode", printer_name="A")
        world.sched.tick()
        out = self._tools(monkeypatch, server, a)["look_at_plate"](person_says="bed clear")
        assert out["success"] is True, out
        assert out["next_up"]["job_id"] == job
        assert "do not start it again" in out["next"] and "Start the print again" not in out["next"]

    def test_with_nothing_waiting_the_persons_word_is_unchanged(self, world, server, monkeypatch):
        a = world.add("A", "SER-A")
        mark_occupied(a, PART)
        out = self._tools(monkeypatch, server, a)["look_at_plate"](person_says="bed clear")
        assert "next_up" not in out
        assert out["next"] == "The bed is on record as clear for the next few minutes. Start the print again."


class TestTheCommandLineSaysIt:
    def test_the_summary_prints_each_wait_the_plate_to_clear_and_the_plan_once(self, world, server, monkeypatch):
        import kiln.cli.output as output
        from kiln.plugins import queue_tools

        monkeypatch.setattr(output, "RICH_AVAILABLE", False)
        _summary_world(world)
        text = output.format_queue_summary(queue_tools.queue_summary())
        lines = text.splitlines()

        waiting = [ln for ln in lines if ln.startswith("Waiting: ")]
        assert len(waiting) == 3
        assert any("B is printing" in ln for ln in waiting) and any("vase v3" in ln for ln in waiting)
        assert "Clear the plate on: C (kiln plate clear --printer C)" in lines
        assert lines.count(TURN_UPGRADE) == 1
        assert text.count("kiln3d.com/pricing") == 1

    def test_job_detail_prints_the_wait_for_a_queued_job_only(self, monkeypatch):
        import kiln.cli.output as output

        monkeypatch.setattr(output, "RICH_AVAILABLE", False)
        note = {"kind": "plate", "sentence": "bracket is next on A. Clear it."}
        queued = {"id": "j1", "file_name": "bracket.gcode", "status": "queued", "metadata": {"waiting": note}}
        assert "Waiting:  bracket is next on A. Clear it." in output.format_job_detail(queued).splitlines()
        printing = {**queued, "status": "printing"}
        assert "Waiting" not in output.format_job_detail(printing)
