"""The loops that decide "has this print ended" must see a fault correctly.

Promoting a fault to the headline moves the word every one of these loops
compares against, so each one had to be re-pointed -- and each is driven here
rather than asserted about, because a test that inspects ``PrinterState``
instead of running the loop passes whichever way the loop is wired.  Every
test in this file was mutation-checked: revert the accessor in the source and
it fails.

Two accessors, two opposite failure directions, and getting them the wrong
way round is worse than the bug they fix:

* ``confirmed_state`` -- "has it ENDED".  Looks through a fault, never
  through staleness, because an expired reading is not evidence that anything
  finished and a watch closed on one is a watch closed on a live print.
* ``effective_state`` -- "might it be BUSY".  Looks through both, because a
  refused action is an inconvenience and acting on an occupied machine is a
  crash.

The classes below are split on exactly that line.
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

import pytest

from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
)

#: The measured A1 fault: "failed to extrude the filament".
FAULT = 302022663


@pytest.fixture(autouse=True)
def _no_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ``send_gcode`` calls in one file otherwise trip the tool limiter,
    and the second test then passes on a refusal that has nothing to do with
    the mid-print block it is meant to be checking."""
    from kiln.server import _tool_limiter

    monkeypatch.setattr(_tool_limiter, "check", lambda *a, **kw: None)


def _state(
    run: PrinterStatus,
    *,
    fault: bool = False,
    stale: bool = False,
    connected: bool = True,
) -> PrinterState:
    """A reading of a machine doing *run*, optionally faulted and/or stale."""
    kwargs: dict[str, Any] = {}
    if stale:
        kwargs["state_age_seconds"] = 1396.0
        kwargs["state_stale_after_seconds"] = 60.0
    return PrinterState(
        connected=connected,
        state=run,
        print_error=FAULT if fault else None,
        **kwargs,
    )


def _adapter(state: PrinterState, job: JobProgress | None = None) -> mock.MagicMock:
    a = mock.MagicMock()
    a.name = "test-printer"
    a.capabilities = PrinterCapabilities(can_snapshot=False)
    a.get_state.return_value = state
    a.get_job.return_value = job or JobProgress(
        completion=12.0, file_name="plate_1.3mf"
    )
    return a


# ---------------------------------------------------------------------------
# "Has it ended" -- through a fault, never through silence
# ---------------------------------------------------------------------------


class TestTheEndingQuestion:
    """A fault must not end a print; silence must not end one either."""

    def test_the_accessor_answers_both_halves(self) -> None:
        """The contract the loops below depend on, stated once."""
        faulted = _state(PrinterStatus.PRINTING, fault=True)
        silent = _state(PrinterStatus.IDLE, stale=True)

        # A fault took the headline, but the machine is printing.
        assert faulted.state is PrinterStatus.ERROR
        assert faulted.confirmed_state is PrinterStatus.PRINTING
        # An expired reading confirms nothing, whatever it last said.
        assert silent.effective_state is PrinterStatus.IDLE
        assert silent.confirmed_state is PrinterStatus.STALE

    def test_the_first_layer_watch_survives_a_fault(self) -> None:
        """The A1 raises a clumping fault on flat first layers.

        Which is exactly when this monitor is looking.  Closing the watch
        there abandons the print it was opened for.
        """
        from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

        monitor = FirstLayerMonitor(
            _adapter(_state(PrinterStatus.PRINTING, fault=True)),
            "test-printer",
            policy=MonitorPolicy(
                first_layer_delay_seconds=0,
                first_layer_check_count=1,
                first_layer_interval_seconds=0,
                monitoring_mode="telemetry",
            ),
        )

        result = monitor.monitor()

        assert result.outcome != "print_ended"

    def test_the_first_layer_watch_still_sits_through_silence(self) -> None:
        """The rule the fault fix must not trample.

        A reading that has expired over an idle run state is not evidence the
        print finished.  ``effective_state`` would say IDLE here and close
        the watch; that is why these loops do not use it.
        """
        from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

        monitor = FirstLayerMonitor(
            _adapter(_state(PrinterStatus.IDLE, stale=True)),
            "test-printer",
            policy=MonitorPolicy(
                first_layer_delay_seconds=0,
                first_layer_check_count=1,
                first_layer_interval_seconds=0,
                monitoring_mode="telemetry",
            ),
        )

        result = monitor.monitor()

        assert result.outcome != "print_ended"

    def test_the_first_layer_watch_still_stops_when_the_print_really_ends(
        self,
    ) -> None:
        """Guard on the other side: it must not become un-endable."""
        from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

        monitor = FirstLayerMonitor(
            _adapter(_state(PrinterStatus.IDLE)),
            "test-printer",
            policy=MonitorPolicy(
                first_layer_delay_seconds=0,
                first_layer_check_count=1,
                first_layer_interval_seconds=0,
                monitoring_mode="telemetry",
            ),
        )

        result = monitor.monitor()

        assert result.outcome == "print_ended"

    def test_await_completion_does_not_call_a_faulted_print_over(self) -> None:
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.await_print_completion(timeout=1, poll_interval=1)

        assert out["outcome"] not in ("completed", "failed")

    def test_await_completion_reports_a_faulted_idle_machine_as_done(self) -> None:
        """The other half: a job whose machine latched a fault after it ended
        must still get an outcome, not fall between both branches."""
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(
            _state(PrinterStatus.IDLE, fault=True), JobProgress(file_name="p.3mf")
        )
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.await_print_completion(timeout=30, poll_interval=1)

        assert out["outcome"] == "completed"

    def test_the_cli_wait_does_not_call_a_faulted_print_over(self) -> None:
        """`kiln printer wait` must not report a running print as finished."""
        from click.testing import CliRunner

        from kiln.cli.main import cli

        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            out = CliRunner().invoke(
                cli, ["wait", "--interval", "1", "--timeout", "2", "--json"]
            )

        # Neither ending branch may fire: the print is still running.
        assert "final_state" not in (out.output or "")

    def test_the_cli_wait_reports_a_faulted_idle_machine_as_finished(self) -> None:
        """The branch the fault moved: a machine that latched a code after
        the print ended still has to produce an ending."""
        from click.testing import CliRunner

        from kiln.cli.main import cli

        adapter = _adapter(_state(PrinterStatus.IDLE, fault=True))
        with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
            out = CliRunner().invoke(
                cli, ["wait", "--interval", "1", "--timeout", "5", "--json"]
            )

        # It reached an ending at all -- before, a faulted-idle machine
        # matched neither branch and the wait ran to its timeout.  The label
        # it prints is the headline, which is the fault: that is the thing
        # the person waiting needs to see.
        assert '"final_state"' in (out.output or "")
        assert '"error"' in (out.output or "")

    def test_the_background_watch_survives_a_fault(self) -> None:
        """The watch a user opens on a print, closed by a clumping probe."""
        from kiln.plugins.monitoring_tools import _PrintWatcher

        watcher = _PrintWatcher(
            "w1",
            _adapter(_state(PrinterStatus.PRINTING, fault=True)),
            "test-printer",
            snapshot_interval=9999,
            poll_interval=1,
            timeout=1,
            stall_timeout=0,
        )
        # Without this the loop reads elapsed from epoch 0 and times out on
        # its first tick, which would make either assertion below pass for
        # a reason that has nothing to do with the fault.
        watcher._start_time = time.time()
        watcher._run()

        # It ran out its (1s) timeout instead of declaring the print over.
        assert (watcher._result or {}).get("outcome") == "timeout"

    def test_the_background_watch_still_ends_on_a_real_ending(self) -> None:
        """Guard on the other side: it must not become un-endable."""
        from kiln.plugins.monitoring_tools import _PrintWatcher

        watcher = _PrintWatcher(
            "w2",
            _adapter(_state(PrinterStatus.ERROR)),
            "test-printer",
            snapshot_interval=9999,
            poll_interval=1,
            timeout=30,
            stall_timeout=0,
        )
        # Without this the loop reads elapsed from epoch 0 and times out on
        # its first tick, which would make either assertion below pass for
        # a reason that has nothing to do with the fault.
        watcher._start_time = time.time()
        watcher._run()

        assert (watcher._result or {}).get("outcome") == "failed"

    def test_the_lifecycle_wrap_does_not_file_a_running_print_as_failed(
        self,
    ) -> None:
        """The worst of them, and it fires on EVERY status read.

        A fault mid-print made "printing -> error" a terminal transition, so
        the running print was recorded as failed -- and the idempotency
        ledger then refused the real ending, filing a print that faulted and
        recovered as a failure for good.
        """
        from kiln.printers.base import _feed_outcome_lifecycle

        asked: list[tuple] = []

        def _spy(prev, new):
            asked.append((prev, new))
            return False

        with (
            mock.patch("kiln.auto_record_hook.is_terminal_transition", _spy),
            mock.patch(
                "kiln.auto_record_hook.observe_state", return_value="printing"
            ),
            mock.patch("kiln.auto_record_hook.reconcile_pending_outcomes"),
        ):
            adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
            adapter._kiln_outcome_delegated = False
            _feed_outcome_lifecycle(adapter, adapter.get_state.return_value)

        assert asked, "the lifecycle wrap did not run"
        # The edge it was asked about must be the RUN state, never the fault.
        assert all(new != "error" for _prev, new in asked), asked


# ---------------------------------------------------------------------------
# "Might it be busy" -- through everything, fail closed
# ---------------------------------------------------------------------------


class TestTheBusyQuestion:
    """Every refusal that protects a machine mid-job."""

    def test_a_mid_print_z_home_is_still_blocked_on_a_faulted_print(self) -> None:
        """Homing Z mid-print drives the nozzle through the part."""
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.send_gcode("G28 Z")

        assert out["success"] is False
        # The mid-print refusal specifically -- a generic failure would let
        # this pass while the block itself had stopped matching.
        assert out["error"]["code"] == "GCODE_MIDPRINT_Z_HOME"
        adapter.send_gcode.assert_not_called()

    def test_a_mid_print_z_home_is_blocked_on_a_silent_printer_too(self) -> None:
        """Reading through staleness here is the conservative direction."""
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(_state(PrinterStatus.PRINTING, stale=True))
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.send_gcode("G28 Z")

        assert out["success"] is False
        assert out["error"]["code"] == "GCODE_MIDPRINT_Z_HOME"

    def test_a_filament_op_is_still_refused_during_a_faulted_print(self) -> None:
        from kiln.printers.base import PrinterAdapter, PrinterError

        adapter = mock.MagicMock()
        adapter.get_state.return_value = _state(PrinterStatus.PRINTING, fault=True)

        with pytest.raises(PrinterError, match="while a print is running"):
            PrinterAdapter._prepare_filament_op(
                adapter,
                "load",
                slot=None,
                material=None,
                temperature=None,
                length_mm=None,
            )

    def test_a_paused_faulted_print_still_warns_the_extruder_is_over_the_part(
        self,
    ) -> None:
        """A pause caused BY a fault is the commonest paused case there is."""
        from kiln.printers.base import PrinterAdapter

        adapter = mock.MagicMock()
        adapter.get_state.return_value = _state(PrinterStatus.PAUSED, fault=True)
        adapter._filament_material_window.return_value = (200.0, 220.0, "test")

        plan = PrinterAdapter._prepare_filament_op(
            adapter,
            "purge",
            slot=None,
            material=None,
            temperature=None,
            length_mm=50.0,
        )

        assert plan.printer_paused is True

    def test_resume_verification_is_not_fooled_by_a_fault(self) -> None:
        """A fault landing during the pause must not read as "it resumed"."""
        from kiln.printers.base import PrinterAdapter, PrintResult

        adapter = mock.MagicMock()
        adapter._RESUME_VERIFY_TIMEOUT = 0.0
        adapter._RESUME_VERIFY_INTERVAL = 0.0
        adapter.get_state.return_value = _state(PrinterStatus.PAUSED, fault=True)

        out = PrinterAdapter._verify_resume_took(
            adapter, PrintResult(success=True, message="sent")
        )

        assert out.success is False
        assert "still reports paused" in out.message

    def test_the_pause_keep_alive_is_not_ended_by_a_fault(self) -> None:
        """A fault arriving during a pause is not the pause ending."""
        assert (
            _state(PrinterStatus.PAUSED, fault=True).confirmed_state
            is PrinterStatus.PAUSED
        )


# ---------------------------------------------------------------------------
# The readers that hold a WORD, not an object
# ---------------------------------------------------------------------------


class TestTheSerialisedWord:
    """A classifier handed only the headline cannot see through a promotion.

    Neither ``stale`` nor ``error`` appears in any hand-written busy-word set
    in this codebase, so a printer that went silent or faulted mid-job fell
    out of every "this machine is working" listing at once.  The object knew;
    the word did not.
    """

    def test_the_row_helper_sees_through_both_promotions(self) -> None:
        from kiln.printers.base import row_run_state

        faulted = _state(PrinterStatus.PRINTING, fault=True).to_dict()
        silent = _state(PrinterStatus.PRINTING, stale=True).to_dict()
        plain = _state(PrinterStatus.PRINTING).to_dict()

        assert row_run_state(faulted) == "printing"
        assert row_run_state(silent) == "printing"
        assert row_run_state(plain) == "printing"

    def test_the_fleet_row_carries_what_the_classifiers_need(self) -> None:
        """The row is built by the registry, not by ``to_dict`` -- it had to
        be taught to carry the displaced state or nothing downstream could
        see through it."""
        from kiln.printers.base import row_run_state, status_is_occupied
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        registry.register(
            "a1", _adapter(_state(PrinterStatus.PRINTING, fault=True))
        )
        rows = registry.get_fleet_status()

        row = rows[0] if isinstance(rows, list) else list(rows.values())[0]
        assert row["state"] == "error"
        assert row_run_state(row) == "printing"
        assert status_is_occupied(row_run_state(row)) is True

    def test_the_trim_guard_will_not_kill_a_watch_on_a_faulted_print(self) -> None:
        """SIGTERM to the servers watching a live print, by a second route.

        The module's own note calls a live print falling out of its
        active-word set "the dangerous case"; a fault does it as surely as
        silence does.
        """
        from kiln import serve_siblings

        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        registry = mock.MagicMock()
        registry.list_all.return_value = {"a1": adapter}

        with mock.patch("kiln.server._get_registry", return_value=registry):
            out = serve_siblings.printing_now()

        assert out["active"], out

    def test_the_camera_does_not_go_dark_at_the_fault(self) -> None:
        """The moment a fault appears is the moment someone wants to look."""
        from kiln.local_monitor import _camera_frame

        status = {"printer": _state(PrinterStatus.PRINTING, fault=True).to_dict()}
        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        adapter.get_snapshot.return_value = b"\xff\xd8\xff\xd9"

        with (
            mock.patch("kiln.server._get_adapter", return_value=adapter),
            mock.patch("kiln.server._get_registry"),
        ):
            frame, note = _camera_frame(None, status)

        assert frame is not None, note

    def test_the_watch_block_says_a_print_is_on_a_faulted_machine(self) -> None:
        from kiln.watch_state import kiln_watch_state

        faulted = _state(PrinterStatus.PRINTING, fault=True).to_dict()
        from kiln.printers.base import row_run_state

        watch = kiln_watch_state(
            "default", adapter=object(), state_word=row_run_state(faulted)
        )

        assert watch["printing"] is True


# ---------------------------------------------------------------------------
# The remaining loops, driven rather than described
# ---------------------------------------------------------------------------


class TestTheLastLoops:
    """Each of these was UNPINNED until it was driven."""

    def test_the_scheduler_does_not_close_a_job_on_a_faulted_print(self) -> None:
        """A fault mid-print is not the job ending."""
        from kiln.scheduler import JobScheduler

        queue, registry, bus = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        registry.get.return_value = _adapter(
            _state(PrinterStatus.PRINTING, fault=True)
        )
        sched = JobScheduler(queue, registry, bus)
        sched._active_jobs = {"j1": "a1"}
        sched._emergency_block_reason = lambda _n: None

        out = sched.tick()

        assert out["completed"] == []
        assert out["failed"] == []

    def test_the_scheduler_closes_a_job_on_a_faulted_idle_machine(self) -> None:
        """The branch the fault actually moved.

        A machine that latched a code after the print ended matched neither
        the idle branch nor the error one, and the job was left open.
        """
        from kiln.scheduler import JobScheduler

        queue, registry, bus = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        registry.get.return_value = _adapter(_state(PrinterStatus.IDLE, fault=True))
        sched = JobScheduler(queue, registry, bus)
        sched._active_jobs = {"j1": "a1"}
        sched._emergency_block_reason = lambda _n: None

        out = sched.tick()

        assert out["completed"] == ["j1"]

    def test_the_scheduler_still_closes_a_job_on_a_real_ending(self) -> None:
        from kiln.scheduler import JobScheduler

        queue, registry, bus = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        registry.get.return_value = _adapter(_state(PrinterStatus.IDLE))
        sched = JobScheduler(queue, registry, bus)
        sched._active_jobs = {"j1": "a1"}
        sched._emergency_block_reason = lambda _n: None

        out = sched.tick()

        assert out["completed"] == ["j1"]

    def test_the_scheduler_does_not_close_a_job_on_a_silent_printer(self) -> None:
        """The rule that predates the fault fix, still standing."""
        from kiln.scheduler import JobScheduler

        queue, registry, bus = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        registry.get.return_value = _adapter(_state(PrinterStatus.IDLE, stale=True))
        sched = JobScheduler(queue, registry, bus)
        sched._active_jobs = {"j1": "a1"}
        sched._emergency_block_reason = lambda _n: None

        out = sched.tick()

        assert out["completed"] == []

    def test_await_completion_does_not_call_a_silent_printer_finished(self) -> None:
        """``effective_state`` here would report a print that went quiet as
        completed; that is why these loops use ``confirmed_state``."""
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(_state(PrinterStatus.IDLE, stale=True))
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.await_print_completion(timeout=1, poll_interval=1)

        assert out["outcome"] not in ("completed", "failed")

    def test_the_background_watch_completes_on_a_faulted_idle_machine(self) -> None:
        """The watch's own IDLE branch, which the fault also moved."""
        import time

        from kiln.plugins.monitoring_tools import _PrintWatcher

        watcher = _PrintWatcher(
            "w3",
            _adapter(_state(PrinterStatus.IDLE, fault=True)),
            "test-printer",
            snapshot_interval=9999,
            poll_interval=1,
            timeout=120,
            stall_timeout=0,
        )
        watcher._start_time = time.time() - 40
        watcher._run()

        assert (watcher._result or {}).get("outcome") == "completed"

    def test_the_background_watch_does_not_complete_on_a_silent_printer(
        self,
    ) -> None:
        """A reading that expired is not evidence the print completed."""
        import time

        from kiln.plugins.monitoring_tools import _PrintWatcher

        watcher = _PrintWatcher(
            "w4",
            _adapter(_state(PrinterStatus.IDLE, stale=True)),
            "test-printer",
            snapshot_interval=9999,
            poll_interval=1,
            timeout=1,
            stall_timeout=0,
        )
        watcher._start_time = time.time() - 40
        watcher._run()

        assert (watcher._result or {}).get("outcome") != "completed"

    def test_the_pause_keep_alive_is_not_dropped_by_a_fault(self) -> None:
        """A fault arriving during a pause must not read as "the pause ended".

        Driven through ``_reassert``, the one place the class decides to
        keep going, so the decision is exercised rather than described.
        """
        import threading

        from kiln.server import _PauseKeepAlive

        keeper = _PauseKeepAlive()
        stop = threading.Event()
        adapter = _adapter(_state(PrinterStatus.PAUSED, fault=True))
        keeper._entries = {
            "k": {"adapter": adapter, "targets": {}, "stop_event": stop}
        }

        assert keeper._reassert("k", stop) is True

    def test_the_pause_keep_alive_still_stops_when_the_pause_ends(self) -> None:
        import threading

        from kiln.server import _PauseKeepAlive

        keeper = _PauseKeepAlive()
        stop = threading.Event()
        adapter = _adapter(_state(PrinterStatus.PRINTING))
        keeper._entries = {
            "k": {"adapter": adapter, "targets": {}, "stop_event": stop}
        }

        assert keeper._reassert("k", stop) is False

    def test_the_watch_block_reports_a_print_through_the_status_door(self) -> None:
        """Driven through ``printer_status``, so the call site is exercised."""
        from unittest.mock import patch

        from kiln import server

        adapter = _adapter(_state(PrinterStatus.PRINTING, fault=True))
        with patch("kiln.server._get_adapter", return_value=adapter):
            out = server.printer_status(detail="lite")

        assert out["printer"]["state"] == "error"
        assert out["kiln_watch"]["printing"] is True


class TestNoMidPrintGuardReadsTheBareHeadline:
    """The rule, pinned once, so the next adapter cannot reopen the hole.

    A guard that refuses an action WHILE A PRINT IS RUNNING is asking what
    the machine is doing, and the headline is not that.  On OctoPrint and
    Moonraker the bare read is a no-op today only because neither adapter
    supplies a staleness budget, so no promotion can reach them -- a
    coincidence, not a design, and one base.py explicitly expects to end.
    """

    @pytest.mark.parametrize(
        "module,cls_name",
        [
            ("kiln.printers.octoprint", "OctoPrintAdapter"),
            ("kiln.printers.moonraker", "MoonrakerAdapter"),
        ],
    )
    def test_the_firmware_update_guard_reads_through_the_headline(
        self, module: str, cls_name: str
    ) -> None:
        """A firmware update mid-print restarts the host; on Moonraker that
        restarts Klipper, which is a hard halt on a live job."""
        import importlib

        from kiln.printers.base import PrinterError

        cls = getattr(importlib.import_module(module), cls_name)
        adapter = mock.MagicMock()
        adapter.get_state.return_value = _state(
            PrinterStatus.PRINTING, fault=True
        )

        with pytest.raises(PrinterError, match="(?i)while printing"):
            cls.update_firmware(adapter)
