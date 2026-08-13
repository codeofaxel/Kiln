"""A stop reaches the machine it names, and is recorded against it.

``cancel_print``, ``pause_print`` and ``resume_print`` took no printer.  Each
resolved ``_get_adapter()`` once and never consulted the registry, so the
agent-facing surface could only ever reach the default printer — while the
CLI has had a global ``--printer`` for as long as it has had these verbs, and
``emergency_stop`` has taken a ``printer_name`` all along.  The safety-critical
verbs were the ones where the agent was weaker than the human.

That is not what Kiln says it does.  ``print_gate._concurrent_fleet_verdict``
states the rule outright: registering, listing and watching printers, and
every safety and control path, work on every machine at every tier, always —
"a licensing rule must never cost a user visibility or control of a hot
machine".  The licensing rule never did.  The signatures did.  Owning a second
printer is free at every tier (``register_printer`` names "a user who simply
owns two machines and uses them one at a time"), so a free user with two
printers is a supported bench on which an agent could not stop one of them.

Aiming a stop is only half of it.  Four pieces of process-wide bookkeeping sat
behind these tools, each hardwired to the default printer, and every one of
them would have acted on the wrong machine the moment the tools could be
aimed:

  * the cancel-intent flag, which is the only thing that tells a cancelled
    print from a finished one on firmware with no "cancelled" state;
  * the pause keep-alive thread, which re-asserts heater targets;
  * the per-printer ``PrintWatchdog``; and
  * the heater watchdog, whose idle tick cools a printer that it believes
    is not printing.

The last two are the sharp ones: a cancel aimed at the second printer would
have blinded the first printer's watchdog and started an idle timer that ends
in ``set_tool_temp(0)`` on a machine mid-print.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from kiln import auto_record_hook as hook
from kiln import server
from kiln.printers import progress_motion as pm
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult
from kiln.registry import PrinterRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lifecycle_state():
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()
    yield
    hook._HOOK_STATE = hook._HookState()
    pm.reset_progress_observations()


@pytest.fixture(autouse=True)
def _isolated_server_state(monkeypatch):
    """A fresh registry, watchdog table and keep-alive per test.

    The per-tool rate limiter is stood down here.  It enforces a 5-second
    MINIMUM INTERVAL on ``cancel_print`` — a sound bound on a runaway agent
    retrying one machine, but it is per tool name, not per printer, so
    stopping a second machine within 5 seconds is refused.  These tests are
    about which machine a stop reaches; the throttle is its own question.
    """
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_pause_keepalive", server._PauseKeepAlive())
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    yield
    # Never leave a daemon thread re-asserting heater targets after a test.
    keepalive = server._pause_keepalive
    with keepalive._lock:
        entries = list(keepalive._entries.values())
        keepalive._entries.clear()
    for entry in entries:
        entry["stop_event"].set()
    for entry in entries:
        entry["thread"].join(timeout=2.0)


class _FakePrinter:
    """A printer that records what it was told, and can be told to refuse.

    Deliberately not a MagicMock: these tests turn on *which object* was
    commanded, and a mock that answers every attribute makes "the wrong
    machine was called" hard to see.
    """

    name = "fake"

    def __init__(self, host: str, state: PrinterStatus = PrinterStatus.PRINTING) -> None:
        self.host = host
        self.serial = ""          # fingerprint falls back to host — distinct per printer
        self._state = state
        self.cancelled = 0
        self.paused = 0
        self.resumed = 0
        self.tool_temps: list[float] = []
        self.bed_temps: list[float] = []
        self.tool_target = 220.0
        self.bed_target = 60.0

    # -- state ------------------------------------------------------------
    def get_state(self) -> PrinterState:
        return PrinterState(
            state=self._state,
            connected=True,
            tool_temp_actual=self.tool_target,
            tool_temp_target=self.tool_target,
            bed_temp_actual=self.bed_target,
            bed_temp_target=self.bed_target,
        )

    # -- control ----------------------------------------------------------
    def cancel_print(self) -> PrintResult:
        self.cancelled += 1
        self._state = PrinterStatus.IDLE
        return PrintResult(success=True, message="cancelled")

    def pause_print(self) -> PrintResult:
        self.paused += 1
        self._state = PrinterStatus.PAUSED
        return PrintResult(success=True, message="paused")

    def resume_print(self, force: bool = False) -> PrintResult:
        self.resumed += 1
        self._state = PrinterStatus.PRINTING
        return PrintResult(success=True, message="resumed")

    def set_tool_temp(self, value: float) -> None:
        self.tool_temps.append(value)

    def set_bed_temp(self, value: float) -> None:
        self.bed_temps.append(value)

    def send_gcode(self, commands) -> None:  # pragma: no cover — chamber path
        pass


def _two_printers(monkeypatch, **kwargs) -> tuple[_FakePrinter, _FakePrinter]:
    """The bench this is about: two machines, the free-tier supported setup.

    ``garage`` is the default printer; ``workshop`` is the one an agent could
    not reach.
    """
    garage = _FakePrinter("192.0.2.10", **kwargs)
    workshop = _FakePrinter("192.0.2.11", **kwargs)
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop


# ---------------------------------------------------------------------------
# The gap itself: the agent-facing verbs can name a machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["cancel_print", "pause_print", "resume_print"])
def test_every_control_verb_can_name_a_printer(verb):
    """The signature IS the capability — this is what was missing.

    ``emergency_stop`` already took a ``printer_name`` and the CLI already had
    ``--printer``.  These three did not, so an agent's only multi-printer stop
    was M112 on everything.
    """
    import inspect

    tool = getattr(server, verb)
    sig = inspect.signature(getattr(tool, "fn", tool))
    assert "printer_name" in sig.parameters
    # Defaulting to None is what keeps every existing caller working.
    assert sig.parameters["printer_name"].default is None


def test_cancel_stops_the_printer_it_names(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)

    out = server.cancel_print(printer_name="workshop")

    assert out["success"] is True
    assert (workshop.cancelled, garage.cancelled) == (1, 0)
    assert out["printer_name"] == "workshop"


def test_pause_and_resume_reach_the_printer_they_name(monkeypatch):
    garage, workshop = _two_printers(monkeypatch)

    assert server.pause_print(printer_name="workshop")["success"] is True
    assert (workshop.paused, garage.paused) == (1, 0)

    assert server.resume_print(printer_name="workshop")["success"] is True
    assert (workshop.resumed, garage.resumed) == (1, 0)


@pytest.mark.parametrize(
    ("verb", "kwargs"),
    [("cancel_print", {}), ("pause_print", {}), ("resume_print", {})],
)
def test_naming_no_printer_still_means_the_default(monkeypatch, verb, kwargs):
    """Every existing caller passes no name and must keep the old behaviour."""
    garage, workshop = _two_printers(monkeypatch)

    out = getattr(server, verb)(**kwargs)

    assert out["success"] is True
    assert out["printer_name"] == "garage"
    assert (workshop.cancelled, workshop.paused, workshop.resumed) == (0, 0, 0)


def test_a_name_kiln_does_not_know_stops_nothing(monkeypatch):
    """Never silently redirect to the default: that stops the wrong print.

    An agent that mistypes a printer name must be told, not quietly obeyed
    against another machine.
    """
    garage, workshop = _two_printers(monkeypatch)

    out = server.cancel_print(printer_name="workshopp")

    assert out["success"] is False
    assert out["error"]["code"] == "PRINTER_NOT_FOUND"
    # The names it does know are in the message, so the retry is one step.
    assert "workshop" in out["error"]["message"]
    assert (garage.cancelled, workshop.cancelled) == (0, 0)


# ---------------------------------------------------------------------------
# The cancel is RECORDED against the machine it stopped
# ---------------------------------------------------------------------------


def test_cancel_files_intent_under_the_machine_it_stopped(monkeypatch):
    """The intent name must come off the adapter that got the cancel.

    ``_resolve_effective_printer_name(None)`` — the old source — answers
    "default", i.e. the same string no matter which machine was stopped.
    """
    _garage, _workshop = _two_printers(monkeypatch)
    filed: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", filed.append)

    server.cancel_print(printer_name="workshop")

    assert filed == ["workshop"]


def test_the_cancelled_print_is_the_one_recorded_cancelled(monkeypatch):
    """End to end through the hook, with the real Bambu lifecycle.

    Two machines, one cancelled and one finishing normally.  Filed under the
    wrong name, the intent is spent on whichever ending arrives first and the
    two outcomes swap: the finished print is recorded cancelled and the
    cancelled one a success.
    """
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    recorded: list[dict] = []
    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: recorded.append(kw) or {"success": True},
    )

    def _bambu(serial: str) -> BambuAdapter:
        return BambuAdapter(host="192.0.2.20", access_code="00000000", serial=serial)

    def _push(adapter, gcode_state: str, *, job: str) -> None:
        payload = {
            "print": {
                "command": "push_status",
                "gcode_state": gcode_state,
                "subtask_name": job,
                "gcode_file": f"/sdcard/{job}.3mf",
                "print_error": 0,
            }
        }
        adapter._on_message(
            None, None, SimpleNamespace(payload=json.dumps(payload).encode())
        )

    garage, workshop = _bambu("00M09A000000001"), _bambu("00M09A000000002")
    registry = server._get_registry()
    registry.register("garage", garage)
    registry.register("workshop", workshop)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    # The cancel command itself is the printer's job; this test is about what
    # the cancel is recorded as, so the MQTT publish is a no-op.
    monkeypatch.setattr(
        BambuAdapter, "cancel_print",
        lambda self: PrintResult(success=True, message="cancelled"),
    )

    _push(garage, "RUNNING", job="bracket")
    _push(workshop, "RUNNING", job="spool-holder")

    server.cancel_print(printer_name="workshop")

    _push(garage, "IDLE", job="bracket")
    _push(workshop, "IDLE", job="spool-holder")

    assert {c["printer_name"]: c["outcome"] for c in recorded} == {
        "garage": "success",
        "workshop": "cancelled",
    }


def test_the_single_printer_bench_still_records_its_cancel(monkeypatch):
    """One printer, no name passed — the case every free install is.

    Pinned separately because it is the regression the named-printer work
    could most easily undo: the intent has to be filed under the name the
    hook consumes it under, which for a registered default is "default".
    """
    printer = _FakePrinter("192.0.2.10")
    server._get_registry().register("default", printer)
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)
    filed: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", filed.append)

    server.cancel_print()

    assert filed == ["default"]


def test_intent_is_filed_before_the_cancel_is_sent(monkeypatch):
    """Ordering, not decoration: the firmware can go idle immediately.

    A cancel that lands before the flag is set races the terminal transition
    it is meant to classify.
    """
    printer = _FakePrinter("192.0.2.10")
    server._get_registry().register("default", printer)
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)

    order: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", lambda n: order.append("intent"))
    original = _FakePrinter.cancel_print

    def _spy(self):
        order.append("cancel")
        return original(self)

    monkeypatch.setattr(_FakePrinter, "cancel_print", _spy)

    server.cancel_print()

    assert order == ["intent", "cancel"]


# ---------------------------------------------------------------------------
# Per-printer bookkeeping: the pause keep-alive
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_pause_keeps_its_own_machine_warm_and_no_other(monkeypatch):
    """The keep-alive re-asserts on the adapter it was started with.

    As a process-wide singleton whose loop called ``_get_adapter()``, pausing
    the second printer spawned a thread that pushed the second printer's
    targets onto the FIRST — reheating an idle machine, or fighting a running
    print's own temperatures, for as long as the pause lasted.
    """
    monkeypatch.setattr(server, "_PAUSE_KEEPALIVE_INTERVAL_S", 0.01)
    garage, workshop = _two_printers(monkeypatch, state=PrinterStatus.PAUSED)

    server.pause_print(printer_name="workshop")
    try:
        assert _wait_for(lambda: len(workshop.tool_temps) >= 2)
        assert garage.tool_temps == []
        assert garage.bed_temps == []
    finally:
        server._pause_keepalive.stop(workshop)


def test_resume_stops_only_its_own_keep_alive(monkeypatch):
    """Two paused machines; resuming one must not un-warm the other."""
    garage, workshop = _two_printers(monkeypatch, state=PrinterStatus.PAUSED)

    server.pause_print(printer_name="garage")
    server.pause_print(printer_name="workshop")
    assert server._pause_keepalive.is_running(garage)
    assert server._pause_keepalive.is_running(workshop)

    try:
        server.resume_print(printer_name="workshop")

        assert not server._pause_keepalive.is_running(workshop)
        assert server._pause_keepalive.is_running(garage)
    finally:
        server._pause_keepalive.stop(garage)
        server._pause_keepalive.stop(workshop)


def test_cancel_stops_only_its_own_keep_alive(monkeypatch):
    garage, workshop = _two_printers(monkeypatch, state=PrinterStatus.PAUSED)

    server.pause_print(printer_name="garage")
    server.pause_print(printer_name="workshop")

    try:
        server.cancel_print(printer_name="workshop")

        assert not server._pause_keepalive.is_running(workshop)
        assert server._pause_keepalive.is_running(garage)
    finally:
        server._pause_keepalive.stop(garage)


def test_one_machine_under_two_names_gets_one_keep_alive(monkeypatch):
    """The server registers the active printer as "default" AND its config
    name.  Keyed by name that is two threads on one machine, both re-asserting
    into the same firmware."""
    printer = _FakePrinter("192.0.2.10", state=PrinterStatus.PAUSED)
    registry = server._get_registry()
    registry.register("default", printer)
    registry.register("garage", printer)
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)

    try:
        assert server.pause_print(printer_name="default")["keep_alive"][
            "started_new_thread"
        ] is True
        assert server.pause_print(printer_name="garage")["keep_alive"][
            "started_new_thread"
        ] is False
        assert len(server._pause_keepalive._entries) == 1
        # And stopping under either name stops the one machine's thread.
        server._pause_keepalive.stop(printer)
        assert not server._pause_keepalive.is_running(printer)
    finally:
        server._pause_keepalive.stop(printer)


# ---------------------------------------------------------------------------
# Per-printer bookkeeping: the two watchdogs
# ---------------------------------------------------------------------------


def test_cancel_leaves_another_printers_watchdog_running(monkeypatch):
    """Tearing down the wrong ``PrintWatchdog`` blinds a live print.

    ``_stop_print_watchdog()`` with no name resolves the DEFAULT printer, so a
    cancel aimed elsewhere stopped the anomaly watch on the machine that was
    still printing and left the cancelled one watched.
    """
    _garage, _workshop = _two_printers(monkeypatch)
    stopped: list[str] = []
    server._print_watchdogs["garage"] = SimpleNamespace(
        stop=lambda timeout=None: stopped.append("garage")
    )
    server._print_watchdogs["workshop"] = SimpleNamespace(
        stop=lambda timeout=None: stopped.append("workshop")
    )

    server.cancel_print(printer_name="workshop")

    assert stopped == ["workshop"]
    assert "garage" in server._print_watchdogs


def test_cancel_does_not_tell_the_heater_watchdog_someone_elses_print_ended(
    monkeypatch,
):
    """The heater watchdog watches the default printer and nothing else.

    Its idle tick checks heater targets but never re-reads whether a job is
    running, so ``notify_print_ended()`` on a cancel aimed at another machine
    starts a timer that ends in ``set_tool_temp(0)`` on the default printer
    mid-print.
    """
    _garage, _workshop = _two_printers(monkeypatch)
    notified: list[str] = []
    monkeypatch.setattr(
        server, "_get_heater_watchdog",
        lambda: SimpleNamespace(notify_print_ended=lambda: notified.append("ended")),
    )

    server.cancel_print(printer_name="workshop")
    assert notified == []

    # Cancelling the machine it IS watching still notifies it.
    server.cancel_print(printer_name="garage")
    assert notified == ["ended"]


# ---------------------------------------------------------------------------
# Emergency stops are endings too — and must not be recorded as successes
# ---------------------------------------------------------------------------
#
# An M112 has no "cancelled" gcode_state: the printer lands on error or
# idle, and an idle with no intent on record is inferred to be a natural
# finish.  cancel_print had this bug and got the intent flag; the emergency
# paths never did, so the one stop reserved for the WORST endings — thermal
# runaway, collision, spaghetti — wrote those prints into the learning DB
# as successes.  Four doors reach an e-stop: the emergency_stop tool,
# emergency_trip_input, and the CLI's estop all route through
# EmergencyCoordinator.emergency_stop (stop-all loops through it), and the
# PrintWatchdog halts its adapter directly.  Two seams, both pinned here.


def _fresh_coordinator(monkeypatch):
    from kiln.emergency import EmergencyCoordinator

    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    return EmergencyCoordinator()


def test_coordinator_estop_files_intent_before_the_halt(monkeypatch):
    """Ordering is the contract: the halt can end the job instantly."""
    _garage, workshop = _two_printers(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", lambda n: order.append(f"intent:{n}"))
    original_send = _FakePrinter.send_gcode

    def _spy(self, commands):
        order.append("halt")
        return original_send(self, commands)

    monkeypatch.setattr(_FakePrinter, "send_gcode", _spy)
    workshop.emergency_stop = lambda: (_ for _ in ()).throw(RuntimeError("no hw estop"))

    coord = _fresh_coordinator(monkeypatch)
    coord.emergency_stop("workshop")

    assert order[0] == "intent:workshop"
    assert "halt" in order


def test_estop_intent_uses_the_lifecycle_name_not_the_alias(monkeypatch):
    """One machine, two registry names — the intent must use the stamped one.

    The server registers the active printer as "default" AND its config
    name, and the outcome lifecycle keys on the LAST stamp.  An e-stop
    aimed at the "default" alias that filed intent under "default" would
    never be consumed — the stop would be recorded a success again, which
    is the bug this whole file exists to keep dead.
    """
    printer = _FakePrinter("192.0.2.10")
    registry = server._get_registry()
    registry.register("default", printer)
    registry.register("garage", printer)   # stamp ends up "garage"
    filed: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", filed.append)

    coord = _fresh_coordinator(monkeypatch)
    coord.emergency_stop("default")

    assert filed == ["garage"]


def test_estop_on_an_unregistered_name_still_files_an_intent(monkeypatch):
    """A latch can exist for a printer the registry never met this boot.

    The G-code cannot be delivered (no adapter), but the record and the
    intent still happen — under the caller's name, which is the best name
    there is.
    """
    filed: list[str] = []
    monkeypatch.setattr(hook, "register_cancel_intent", filed.append)

    coord = _fresh_coordinator(monkeypatch)
    coord.emergency_stop("ghost-printer")

    assert filed == ["ghost-printer"]


def test_watchdog_estop_is_not_recorded_a_success(monkeypatch):
    """The full path: red flag → adapter-level M112 → idle → "cancelled".

    The PrintWatchdog bypasses the coordinator on purpose (it holds the
    adapter; nothing stands between a red flag and the halt), so it files
    its own intent.  This is the print the learning DB most needs to know
    went wrong — a spaghetti detection recorded as a success poisons every
    similarity lookup that follows.
    """
    from kiln.print_watchdog import Flag, PrintWatchdog
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    recorded: list[dict] = []
    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: recorded.append(kw) or {"success": True},
    )

    printer = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000003",
    )
    server._get_registry().register("workshop", printer)

    def _push(gcode_state: str) -> None:
        payload = {
            "print": {
                "command": "push_status",
                "gcode_state": gcode_state,
                "subtask_name": "bracket",
                "gcode_file": "/sdcard/bracket.3mf",
                "print_error": 0,
            }
        }
        printer._on_message(
            None, None, SimpleNamespace(payload=json.dumps(payload).encode())
        )

    _push("RUNNING")

    wd = PrintWatchdog(adapter=printer, poll_interval_sec=999, on_anomaly=lambda f: None)
    wd._trip(Flag(kind="red", rule="tool_drop", message="nozzle dive", timestamp=0.0))

    _push("IDLE")

    assert [(c["printer_name"], c["outcome"]) for c in recorded] == [
        ("workshop", "cancelled")
    ]


def test_a_spent_intent_cannot_poison_the_next_jobs_ending(monkeypatch):
    """Every terminal transition spends the intent, even ones that don't
    need it — so a stop that surfaced as an error state cannot leave its
    intent behind to flip the NEXT job's honest finish into "cancelled".
    """
    from kiln.printers.bambu import BambuAdapter

    monkeypatch.setattr(BambuAdapter, "_ensure_mqtt", lambda self: None)
    recorded: list[dict] = []
    import kiln.plugins.learning_tools as lt

    monkeypatch.setattr(
        lt, "record_print_outcome",
        lambda **kw: recorded.append(kw) or {"success": True},
    )

    printer = BambuAdapter(
        host="192.0.2.20", access_code="00000000", serial="00M09A000000004",
    )
    server._get_registry().register("garage", printer)

    def _push(gcode_state: str, *, job: str) -> None:
        payload = {
            "print": {
                "command": "push_status",
                "gcode_state": gcode_state,
                "subtask_name": job,
                "gcode_file": f"/sdcard/{job}.3mf",
                "print_error": 0,
            }
        }
        printer._on_message(
            None, None, SimpleNamespace(payload=json.dumps(payload).encode())
        )

    # Job 1: a stop whose ending surfaced as an ERROR state, not idle.
    _push("RUNNING", job="bracket")
    hook.register_cancel_intent("garage")
    _push("FAILED", job="bracket")

    # Job 2 ends honestly, within the intent TTL.
    _push("RUNNING", job="spool-holder")
    _push("FINISH", job="spool-holder")

    by_job = {c["job_id"]: c["outcome"] for c in recorded}
    assert by_job["bracket"] == "failed"       # firmware's word wins
    assert by_job["spool-holder"] == "success"  # not "cancelled"
