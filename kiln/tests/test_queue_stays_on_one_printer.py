"""Below Kiln Business the queue works on one printer and stays there.

Until this change the one-printer rule held for every START (never two
printers printing at once) and every COMMAND (Kiln drives one machine), and
the engagement behind both ends when its print ends.  So the queue, which
starts prints with nobody there, started the next job on whichever printer it
was pinned to as soon as the last print ended: queue jobs for ten printers,
walk away, and each started onto a fresh plate in turn.  Never parallel, but
an unattended farm, which is the fleet tier's value.  A person moving from one
printer to the next stays free; the queue moving on its own does not.

The scheduler is driven through ``tick()`` with fake printers (a serial each,
so the engagement record can name them) in a per-test ``KILN_HOME``.  The
fake printers' ``start_print`` is a mock, so the start chokepoint's own
``engage(reason="started")`` is called by hand where a test needs a print a
PERSON started.

A/B: run against the scheduler with ``_queue_on`` returning ``None`` (the
pre-change path), the tests marked "hop" fail: B's job starts once A's print
ends -- "AssertionError: assert 1 == 0" at ``b.start_print.call_count``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock

import pytest

from kiln.events import EventBus, EventType
from kiln.printers import engagement
from kiln.printers.base import JobProgress, PrinterCapabilities, PrinterState, PrinterStatus, PrintResult
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import QUEUE_ON_ONE_PRINTER_CODE, TURN_UPGRADE, JobScheduler


def _printer(name: str, serial: str, state: PrinterStatus = PrinterStatus.IDLE) -> MagicMock:
    adapter = MagicMock()
    type(adapter).name = PropertyMock(return_value=name)
    type(adapter).capabilities = PropertyMock(return_value=PrinterCapabilities())
    adapter.serial = serial
    adapter.snapshot_source = None
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
    engagement._verify_cache.clear()


@pytest.fixture()
def world():
    queue, registry, bus, clock = PrintQueue(), PrinterRegistry(), EventBus(), _Clock()
    sched = JobScheduler(queue, registry, bus, poll_interval=0.1, max_retries=2)
    sched._clock = clock

    def add(name: str, serial: str, state: PrinterStatus = PrinterStatus.IDLE) -> MagicMock:
        adapter = _printer(name, serial, state)
        registry.register(name, adapter)
        return adapter

    return SimpleNamespace(queue=queue, registry=registry, bus=bus, clock=clock, sched=sched, add=add)


@pytest.fixture()
def business(monkeypatch):
    from kiln import licensing

    monkeypatch.setattr(licensing, "get_tier", lambda: licensing.LicenseTier.BUSINESS)


def _note(queue: PrintQueue, job_id: str) -> dict | None:
    return (queue.get_job(job_id).metadata or {}).get("waiting")


def _waiting_events(bus: EventBus) -> list:
    return list(reversed(bus.recent_events(EventType.JOB_WAITING, limit=1000)))


def _finish(world, adapter: MagicMock) -> None:
    """Run the print the queue started on *adapter* to its end: seen
    printing, then idle."""
    _set_state(adapter, PrinterStatus.PRINTING)
    world.clock.advance(5)
    world.sched.tick()
    _set_state(adapter, PrinterStatus.IDLE)
    world.clock.advance(5)
    world.sched.tick()


def _a_then_b(world):
    a, b = world.add("A", "SER-A"), world.add("B", "SER-B")
    job_a = world.queue.submit("bracket.gcode", printer_name="A")
    job_b = world.queue.submit("lid.gcode", printer_name="B")
    return a, b, job_a, job_b


class TestTheQueueStaysPut:
    def test_hop_a_job_for_another_printer_never_starts_on_its_own(self, world):
        a, b, job_a, job_b = _a_then_b(world)
        world.sched.tick()
        assert a.start_print.call_count == 1
        assert b.start_print.call_count == 0, "the queue took A this pass; B is not tried beside it"

        _finish(world, a)
        assert world.queue.get_job(job_a).status == JobStatus.COMPLETED
        for _ in range(20):
            world.clock.advance(60)
            world.sched.tick()
        assert b.start_print.call_count == 0, "A's print ended; the queue stays on A"
        assert world.queue.get_job(job_b).status == JobStatus.QUEUED
        assert world.sched._retry_counts.get(job_b, 0) == 0, "waiting is not failing"

    def test_hop_the_waiting_job_says_where_the_queue_is_and_how_to_move_it(self, world):
        a, _b, _job_a, job_b = _a_then_b(world)
        world.sched.tick()
        _finish(world, a)
        note = _note(world.queue, job_b)
        assert note["kind"] == "switch" and note["code"] == QUEUE_ON_ONE_PRINTER_CODE
        assert note["printers"] == ["B"] and note["on"] == "A" and note["upgrade"] == TURN_UPGRADE
        assert note["sentence"] == (
            "Kiln's queue works on one printer at a time on this plan, and it is on A. lid is for B, so it "
            "waits until you move Kiln there: tell your assistant you are done with A (hand it back), or "
            "start a print on B yourself."
        )
        switch = [e for e in _waiting_events(world.bus) if e.data["kind"] == "switch"]
        assert len(switch) == 1, "announced once, not every pass"
        assert switch[0].data["message"].endswith(TURN_UPGRADE)

    def test_hop_an_unassigned_job_goes_only_to_the_queues_printer(self, world):
        a, b = world.add("A", "SER-A"), world.add("B", "SER-B")
        world.queue.submit("bracket.gcode", printer_name="A")
        loose = world.queue.submit("hook.gcode")
        world.sched.tick()
        _set_state(a, PrinterStatus.PRINTING)
        for _ in range(3):
            world.clock.advance(30)
            world.sched.tick()
        assert b.start_print.call_count == 0, "B is idle, but the queue is on A"
        _set_state(a, PrinterStatus.IDLE)
        world.clock.advance(30)
        world.sched.tick()
        assert world.queue.get_job(loose).status == JobStatus.PRINTING
        assert a.start_print.call_count == 2 and b.start_print.call_count == 0


class TestAPersonMovesIt:
    def test_handing_the_printer_back_moves_the_queue_on(self, world):
        a, b, _job_a, job_b = _a_then_b(world)
        world.sched.tick()
        _finish(world, a)
        report = engagement.hand_back(a)
        assert report["released"] is True and report["queue_only"] is True
        world.clock.advance(5)
        world.sched.tick()
        assert b.start_print.call_count == 1
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING
        assert engagement.machine_id(b) == engagement.queue_printer()["machine"], "the queue is now on B"

    def test_a_print_a_person_starts_elsewhere_takes_the_queue_with_it(self, world):
        a, b, _job_a, job_b = _a_then_b(world)
        world.sched.tick()
        _finish(world, a)
        engagement.engage(b, None, reason="started")  # what the start chokepoint records
        assert engagement.queue_printer()["machine"] == engagement.machine_id(b)
        world.clock.advance(5)
        world.sched.tick()
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING

    def test_watching_a_print_elsewhere_does_not_move_it(self, world):
        a, b, _job_a, _job_b = _a_then_b(world)
        world.sched.tick()
        engagement.engage(b, None, reason="watching")
        assert engagement.queue_printer()["machine"] == engagement.machine_id(a)

    def test_an_empty_queue_is_on_no_printer(self, world):
        a = world.add("A", "SER-A")
        b = world.add("B", "SER-B")
        world.queue.submit("bracket.gcode", printer_name="A")
        world.sched.tick()
        _finish(world, a)
        world.clock.advance(5)
        world.sched.tick()
        assert engagement.queue_printer() is None, "nothing left: the queue lets go of A"
        job_b = world.queue.submit("lid.gcode", printer_name="B")
        world.sched.tick()
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING, "a job sent to an empty queue starts where it was sent"
        assert b.start_print.call_count == 1

    def test_a_printer_no_longer_set_up_lets_the_queue_go(self, world):
        a, b, _job_a, job_b = _a_then_b(world)
        world.sched.tick()
        _finish(world, a)
        world.registry.unregister("A")
        world.clock.advance(5)
        world.sched.tick()
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING
        assert b.start_print.call_count == 1


class TestBusiness:
    def test_on_the_fleet_tier_the_queue_runs_every_printer(self, world, business):
        a, b, job_a, job_b = _a_then_b(world)
        world.sched.tick()
        assert a.start_print.call_count == 1 and b.start_print.call_count == 1
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING
        assert engagement.queue_printer() is None, "nothing is recorded where nothing is held"


class TestTheTools:
    def test_queue_summary_says_the_plan_once(self, world, monkeypatch):
        import kiln.server as srv
        from kiln.plugins import queue_tools

        monkeypatch.setattr(srv, "_get_queue", lambda: world.queue)
        monkeypatch.setattr(srv, "_get_registry", lambda: world.registry)
        monkeypatch.setattr(srv, "_scheduler", world.sched)
        monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
        a, _b, _job_a, job_b = _a_then_b(world)
        world.queue.submit("hook.gcode", printer_name="B")
        world.sched.tick()
        _finish(world, a)

        out = queue_tools.queue_summary()
        assert [w["kind"] for w in out["waiting"]] == ["switch", "switch"]
        assert out["upgrade"] == TURN_UPGRADE
        assert all("upgrade" not in w for w in out["waiting"])
        status = queue_tools.job_status(job_b)
        assert status["waiting"]["kind"] == "switch"

    def test_hand_back_printer_moves_the_queue(self, world, monkeypatch):
        import kiln.server as srv
        from kiln.plugins import printer_management_tools

        monkeypatch.setattr(srv, "_get_registry", lambda: world.registry)
        monkeypatch.setattr(srv, "_check_auth", lambda scope: None)

        class _FakeMCP:
            def __init__(self) -> None:
                self.tools: dict = {}

            def tool(self, *args, **kwargs):
                def wrap(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return wrap

        mcp = _FakeMCP()
        printer_management_tools.plugin.register(mcp)
        a, b, _job_a, job_b = _a_then_b(world)
        world.sched.tick()
        _finish(world, a)

        answer = mcp.tools["hand_back_printer"]("A")
        assert answer["success"] is True and answer["queue_released"] is True
        assert answer["message"] == (
            "Kiln's queue has moved off A. The next job in line starts on its own printer once that plate is clear."
        )
        world.clock.advance(5)
        world.sched.tick()
        assert world.queue.get_job(job_b).status == JobStatus.PRINTING
        assert b.start_print.call_count == 1


def test_asking_which_printer_names_the_queues(world, monkeypatch):
    import kiln.server as srv
    from kiln.plugins import printer_management_tools

    monkeypatch.setattr(srv, "_get_registry", lambda: world.registry)
    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)

    class _FakeMCP:
        def __init__(self) -> None:
            self.tools: dict = {}

        def tool(self, *args, **kwargs):
            def wrap(fn):
                self.tools[fn.__name__] = fn
                return fn

            return wrap

    mcp = _FakeMCP()
    printer_management_tools.plugin.register(mcp)
    a, _b, _job_a, _job_b = _a_then_b(world)
    world.sched.tick()
    _finish(world, a)
    answer = mcp.tools["hand_back_printer"]()
    assert answer["queue_on"] == "A"
    assert answer["message"] == (
        "Kiln is not working with a printer right now. Kiln's queue is on A; hand it back to move the queue to "
        "the next job's printer."
    )


class TestTheFolderWatcher:
    """``kiln ingest watch --auto-queue`` starts files nobody previews on
    whichever printer is idle: the same hop, through the command line."""

    def _run(self, tmp_path, monkeypatch, args):
        from click.testing import CliRunner

        import kiln.cli.main as cli_main
        import kiln.cli.print_gate as cli_gate

        two = {"A": _printer("A", "SER-A"), "B": _printer("B", "SER-B")}
        monkeypatch.setattr(
            cli_main, "_load_fleet_adapters",
            lambda printer_filter=None: ({printer_filter: two[printer_filter]} if printer_filter else dict(two), []),
        )
        armed: list[str] = []
        monkeypatch.setattr(cli_gate, "confirm_standing_auto_print_at_terminal", lambda **kw: armed.append(kw["scope"]))
        watch = tmp_path / "drop"
        watch.mkdir()
        result = CliRunner().invoke(
            cli_main.cli, ["ingest", "watch", "--dir", str(watch), "--auto-queue", "--once", "--json", *args],
        )
        return result, armed

    def test_hop_below_business_it_needs_one_printer(self, tmp_path, monkeypatch):
        result, armed = self._run(tmp_path, monkeypatch, [])
        assert result.exit_code == 1, result.output
        assert "ONE_PRINTER_WATCHER" in result.output
        assert "add --printer NAME (one of A, B)" in result.output
        assert armed == [], "it never arms over every printer"

    def test_hop_the_background_service_is_refused_at_install(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        import kiln.cli.main as cli_main

        monkeypatch.setattr(cli_main, "_list_configured_printer_names", lambda: ["A", "B"])
        watch = tmp_path / "drop"
        watch.mkdir()
        result = CliRunner().invoke(
            cli_main.cli,
            ["ingest", "service", "install", "--dir", str(watch), "--auto-queue",
             "--config-path", str(tmp_path / "svc.json"), "--json"],
        )
        assert result.exit_code == 1 and "ONE_PRINTER_WATCHER" in result.output, result.output
        assert not (tmp_path / "svc.json").exists(), "nothing is installed"

    def test_one_printer_named_arms_as_before(self, tmp_path, monkeypatch):
        result, armed = self._run(tmp_path, monkeypatch, ["--printer", "A"])
        assert "ONE_PRINTER_WATCHER" not in result.output, result.output
        assert armed and armed[0].endswith("A")

    def test_on_the_fleet_tier_it_watches_every_printer(self, tmp_path, monkeypatch, business):
        result, armed = self._run(tmp_path, monkeypatch, [])
        assert "ONE_PRINTER_WATCHER" not in result.output, result.output
        assert armed and armed[0].endswith("A, B")
