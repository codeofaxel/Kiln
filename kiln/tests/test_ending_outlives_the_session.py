"""A print's ending is recorded even when the process that started it is gone.

Inside a live process the watchdog and a Bambu's push thread resolve the
pending row on the ending edge.  Nothing survived the process dying first:
an agent session lasts minutes, a print lasts hours, the CLI exits on the
start, and the next connect found an idle machine and filed ``unknown``.

``kiln.ending_watch`` spawns one detached watcher per machine from the
start template.  It holds nothing while the parent lives (a Bambu rations
its MQTT slots), takes the printer when the parent is gone, polls within
the watched-ending gap so the edge banks as observed, stamps the stopwatch
from the row's wall-clock start so hours bank too, and leaves when nothing
on the machine is pending.

A/B: every test here fails on the tree before kiln.ending_watch existed.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from kiln import ending_watch


@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / ".kiln"))
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    pro = False
    try:
        from kiln_pro.print_learning import auto_record as _pro_auto_record

        pro = _pro_auto_record.uninstall_auto_record_hook()
    except ImportError:
        pass
    yield tmp_path
    if pro:
        _pro_auto_record.install_auto_record_hook()
    monkeypatch.setattr(_p, "_db", None, raising=False)


def _fake_adapter(name: str = "fake-printer", states=(), job_file: str = "bracket.gcode", job_seconds: int = 1800):
    from kiln.printers.base import JobProgress, PrinterAdapter, PrinterState, PrinterStatus, PrintResult

    class _Fake(PrinterAdapter):
        # The printer's own clock freezes at the ending, like Moonraker's,
        # so a watched ending banks its reported duration.
        _DURATION_SEMANTICS = "frozen"

        def __init__(self):
            self._states = list(states)
            self._kiln_registered_name = name
            self.disconnected = False

        def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
            return PrintResult(success=True, message="started")

        def get_state(self):
            word = self._states.pop(0) if len(self._states) > 1 else self._states[0]
            return PrinterState(connected=True, state=PrinterStatus(word))

        def get_job(self):
            return JobProgress(file_name=job_file, print_time_seconds=job_seconds)

        def connect(self): ...
        def disconnect(self):
            self.disconnected = True
        def get_printer_info(self): ...
        def list_files(self): ...
        def upload_file(self, file_path): ...
        def cancel_print(self): ...
        def pause_print(self): ...
        def resume_print(self): ...

    _Fake.name = name
    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


class TestSpawnGuards:
    def test_no_watcher_under_pytest_or_sandbox_or_hosted_or_switch(self, monkeypatch):
        monkeypatch.delenv(ending_watch.TEST_OPT_IN_ENV, raising=False)
        assert ending_watch.should_spawn() is False  # pytest is running
        monkeypatch.setenv(ending_watch.TEST_OPT_IN_ENV, "1")
        assert ending_watch.should_spawn() is True
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        assert ending_watch.should_spawn() is False
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT")
        monkeypatch.setenv(ending_watch.SWITCH_ENV, "0")
        assert ending_watch.should_spawn() is False

    def test_start_print_spawns_one_detached_watcher_per_machine(self, tmp_kiln_env, monkeypatch):
        monkeypatch.setenv(ending_watch.TEST_OPT_IN_ENV, "1")
        spawned: list[list[str]] = []

        class _Proc:
            pid = os.getpid()  # alive, so a second start finds it running

        def _popen(cmd, **kw):
            spawned.append(list(cmd))
            assert kw["start_new_session"] is True
            assert kw["stdin"] is ending_watch.subprocess.DEVNULL
            return _Proc()

        monkeypatch.setattr(ending_watch.subprocess, "Popen", _popen)
        adapter = _fake_adapter(states=("printing",))
        assert adapter.start_print("a.gcode").success
        assert adapter.start_print("b.gcode").success
        assert len(spawned) == 1, "a live watcher covers the second print too"
        cmd = spawned[0]
        assert cmd[1:3] == ["-m", "kiln.ending_watch"]
        assert cmd[cmd.index("--printer") + 1] == "fake-printer"
        assert cmd[cmd.index("--parent") + 1] == str(os.getpid())
        assert (ending_watch.watch_dir() / "fake-printer.pid").read_text() == str(os.getpid())

    def test_a_dead_watcher_pid_is_replaced(self, tmp_kiln_env, monkeypatch):
        monkeypatch.setenv(ending_watch.TEST_OPT_IN_ENV, "1")
        ending_watch.watch_dir().mkdir(parents=True)
        (ending_watch.watch_dir() / "fake-printer.pid").write_text("999999999")
        calls: list[int] = []

        class _Proc:
            pid = 4242

        monkeypatch.setattr(ending_watch.subprocess, "Popen", lambda *a, **k: calls.append(1) or _Proc())
        assert ending_watch.spawn_ending_watch(_fake_adapter(), "x.gcode") == 4242
        assert calls == [1]

    def test_spawn_never_raises_into_a_print(self, tmp_kiln_env, monkeypatch):
        monkeypatch.setenv(ending_watch.TEST_OPT_IN_ENV, "1")

        def _boom(*a, **k):
            raise OSError("no fork for you")

        monkeypatch.setattr(ending_watch.subprocess, "Popen", _boom)
        assert ending_watch.spawn_ending_watch(_fake_adapter(), "x.gcode") is None
        assert _fake_adapter(states=("printing",)).start_print("x.gcode").success


class TestTheWatcher:
    def _open_pending(self, name="fake-printer", file_name="bracket.gcode", started_ago_s=0.0):
        from kiln.persistence import get_db

        job_id = f"start:{name}:{int(time.time() * 1000)}"
        get_db().open_pending_outcome(job_id=job_id, printer_name=name, file_name=file_name)
        if started_ago_s:
            get_db()._conn.execute(
                "UPDATE print_outcomes SET created_at = ? WHERE job_id = ?",
                (time.time() - started_ago_s, job_id),
            )
            get_db()._conn.commit()
        return job_id

    def test_holds_nothing_while_the_parent_lives_and_leaves_when_it_settles(self, tmp_kiln_env):
        from kiln.persistence import get_db

        job_id = self._open_pending()
        resolved: list[str] = []
        ticks = {"n": 0}

        def _sleep(_s):
            ticks["n"] += 1
            if ticks["n"] == 3:
                get_db().save_print_outcome(
                    {"job_id": job_id, "printer_name": "fake-printer", "outcome": "success",
                     "agent_id": "auto", "determined_by": "observed"}
                )

        def _resolve(_name):
            resolved.append(_name)
            return _fake_adapter()

        why = ending_watch.watch("fake-printer", os.getpid(), sleep=_sleep, resolve=_resolve)
        assert why == "settled_by_parent"
        assert resolved == [], "the parent was alive: the watcher never took the printer"

    def test_takes_the_printer_when_the_parent_is_gone_and_records_the_ending(
        self, tmp_kiln_env, monkeypatch
    ):
        """The one that matters: parent dead, print running, then finish --
        the row resolves as success/observed and the duration banks from
        the row's own start time."""
        from kiln.persistence import get_db

        self._open_pending(started_ago_s=1800.0)
        adapter = _fake_adapter(states=("printing", "printing", "idle", "idle", "idle"))
        clock = {"t": 1000.0}

        def _sleep(s):
            clock["t"] += s

        banked: list[tuple[str, float]] = []
        import kiln.daily_stats as _ds

        real = _ds.record_print_hours_for_job

        def _bank(job_label, hours, **kw):
            banked.append((job_label, hours))
            return real(job_label, hours, **kw)

        monkeypatch.setattr(_ds, "record_print_hours_for_job", _bank)
        why = ending_watch.watch(
            "fake-printer", 999999999, sleep=_sleep, clock=lambda: clock["t"],
            resolve=lambda _n: adapter,
        )
        assert why == "settled"
        rows = get_db().list_print_outcomes(printer_name="fake-printer")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"
        assert rows[0]["determined_by"] == "observed"
        assert rows[0]["ended_at"] is not None
        assert adapter.disconnected
        assert banked and 0.45 <= banked[0][1] <= 0.55, banked

    def test_an_idle_first_frame_is_settled_honestly_to_unknown(self, tmp_kiln_env):
        """The parent died and the print ended before the watcher connected:
        the adapter-generic first-frame reconcile files unknown (idle cannot
        tell finished from failed), and the watcher leaves."""
        from kiln.persistence import get_db

        self._open_pending()
        adapter = _fake_adapter(states=("idle",))
        clock = {"t": 0.0}

        def _sleep(s):
            clock["t"] += s

        why = ending_watch.watch(
            "fake-printer", 999999999, sleep=_sleep, clock=lambda: clock["t"],
            resolve=lambda _n: adapter,
        )
        assert why == "settled"
        rows = get_db().list_print_outcomes(printer_name="fake-printer", outcome="unknown")
        assert rows[0]["determined_by"] == "inferred"

    def test_the_stopwatch_is_stamped_from_the_rows_own_start(self, tmp_kiln_env):
        """A Bambu's duration is a Kiln-side stopwatch that lived in the dead
        process.  The watcher restarts it from the row's wall-clock start,
        so the ending it sees banks the real hours."""
        from kiln.printers.progress_motion import job_elapsed_seconds

        adapter = _fake_adapter()
        row = {"created_at": time.time() - 1800.0}
        ending_watch._stamp_start_from_row(adapter, row)
        elapsed = job_elapsed_seconds(adapter, "bracket")
        assert elapsed is not None and 1795 <= elapsed <= 1805

    def test_a_row_nothing_settles_is_left_after_the_grace(self, tmp_kiln_env):
        """An adapter with no reconcile of its own: the watcher does not sit
        on an idle machine forever; the row stays for the next session."""
        adapter = _fake_adapter(states=("idle",))
        clock = {"t": 0.0}

        def _sleep(s):
            clock["t"] += s

        why = ending_watch.watch(
            "fake-printer", 999999999, sleep=_sleep, clock=lambda: clock["t"],
            resolve=lambda _n: adapter,
            pending=lambda _n: [{"job_id": "x", "outcome": "pending", "created_at": time.time()}],
        )
        assert why == "not_printing"
        assert clock["t"] >= ending_watch.NOT_PRINTING_GRACE_S

    def test_an_unreachable_printer_is_retried_with_backoff_then_the_cap(self, tmp_kiln_env):
        self._open_pending()
        adapter = _fake_adapter(states=("printing",))

        def _boom():
            raise ConnectionError("refused")

        adapter.get_state = _boom
        clock = {"t": 0.0}
        waits: list[float] = []

        def _sleep(s):
            waits.append(s)
            clock["t"] += s

        why = ending_watch.watch(
            "fake-printer", 999999999, sleep=_sleep, clock=lambda: clock["t"],
            resolve=lambda _n: adapter, max_watch_s=1000.0,
        )
        assert why == "timed_out"
        assert waits[:4] == [10.0, 20.0, 40.0, 80.0]
        assert max(waits) <= ending_watch.UNREACHABLE_BACKOFF_S[1]

    def test_no_adapter_for_the_name_leaves_quietly(self, tmp_kiln_env):
        self._open_pending()
        assert ending_watch.watch("fake-printer", 999999999, resolve=lambda _n: None) == "no_adapter"
