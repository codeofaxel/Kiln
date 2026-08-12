"""The bridge supervisor: what happens after the bridge process dies.

Measured 2026-08-11 against a real printer: ``kill -9`` on the bridge during a
live print left the printer printing (the machine owns the job) and the relay
answering a correct 409 — and the bridge dead until a human restarted it by
hand.  The bridge's own reconnect loop never covered this; it only ever covered
a dropped socket, which is a failure the process survives to notice.

Every test here drives :func:`supervise` with an injected spawn, clock and
sleep, so the restart policy is exercised without a process, a socket, or a
real second of waiting.
"""
from __future__ import annotations

import json

import kiln.bridge_supervisor as sup


class _Clock:
    """A clock the fake children advance, so ``ran_for`` is under test control."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Child:
    """A scripted bridge process: runs for ``ran_for`` seconds, exits ``code``."""

    def __init__(self, clock: _Clock, ran_for: float, code: int, pid: int = 4242) -> None:
        self._clock = clock
        self._ran_for = ran_for
        self._code = code
        self.pid = pid
        self.terminated = False

    def wait(self) -> int:
        self._clock.t += self._ran_for
        return self._code

    def terminate(self) -> None:
        self.terminated = True


def _spawner(clock: _Clock, script: list[tuple[float, int]]):
    """Spawn children per *script*; the last entry repeats for a crash loop."""
    made: list[_Child] = []

    def spawn() -> _Child:
        ran_for, code = script[min(len(made), len(script) - 1)]
        child = _Child(clock, ran_for, code)
        made.append(child)
        return child

    return spawn, made


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **fields) -> None:
        self.calls.append(fields)

    @property
    def last(self) -> dict:
        return self.calls[-1] if self.calls else {}


def _stop_after(made: list, n: int):
    return lambda: len(made) < n


# --- the measured failure -------------------------------------------------


def test_a_killed_bridge_is_started_again():
    """The whole point: the bridge dies, something puts it back."""
    clock = _Clock()
    spawn, made = _spawner(clock, [(3600.0, -9)])
    sleeps: list[float] = []
    record = _Recorder()

    verdict = sup.supervise(
        spawn=spawn,
        sleep=sleeps.append,
        clock=clock,
        should_continue=_stop_after(made, 2),
        record=record,
    )

    assert verdict == sup.STOPPED
    assert len(made) == 2, "the bridge was killed and never replaced"
    assert sleeps == [1.0]
    assert record.last["restarts"] == 1
    assert record.last["last_exit"]["code"] == -9


def test_a_stop_request_is_not_a_crash():
    """Asked to stop while the child was running — its exit is ours, not a fault."""
    clock = _Clock()
    spawn, made = _spawner(clock, [(10.0, -15)])
    record = _Recorder()

    verdict = sup.supervise(
        spawn=spawn,
        sleep=lambda _s: None,
        clock=clock,
        should_continue=_stop_after(made, 1),
        record=record,
    )

    assert verdict == sup.STOPPED
    assert len(made) == 1
    assert record.calls == [], "a deliberate stop was recorded as a crash"


def test_a_clean_exit_is_respected():
    """Exit 0 means the bridge decided it was done.  Don't argue with it."""
    clock = _Clock()
    spawn, made = _spawner(clock, [(10.0, 0)])

    verdict = sup.supervise(
        spawn=spawn, sleep=lambda _s: None, clock=clock, record=_Recorder(),
    )

    assert verdict == sup.CLEAN_EXIT
    assert len(made) == 1


# --- the crash loop -------------------------------------------------------


def test_a_crash_loop_stops_and_says_why():
    """A bridge that dies seconds after every start is refusing, not crashing.

    Restarting it forever would burn a core and bury the reason (a revoked
    licence, a missing dependency, a broken install).
    """
    clock = _Clock()
    spawn, made = _spawner(clock, [(0.5, 1)])
    sleeps: list[float] = []
    record = _Recorder()

    verdict = sup.supervise(
        spawn=spawn,
        sleep=sleeps.append,
        clock=clock,
        record=record,
        max_rapid_restarts=3,
        healthy_run_s=60.0,
    )

    assert verdict == sup.GAVE_UP
    assert len(made) == 4, "three restarts, then stop"
    assert sleeps == [1.0, 2.0, 4.0]
    assert record.last["gave_up"] is True
    assert record.last["last_exit"]["code"] == 1


def test_a_healthy_run_earns_the_whole_budget_back():
    """A crash after real service is not the tail of an old crash loop."""
    clock = _Clock()
    spawn, made = _spawner(
        clock,
        # two quick deaths, then a run long enough to count, then two more
        [(0.5, 1), (0.5, 1), (120.0, -9), (0.5, 1), (0.5, 1)],
    )
    sleeps: list[float] = []

    verdict = sup.supervise(
        spawn=spawn,
        sleep=sleeps.append,
        clock=clock,
        should_continue=_stop_after(made, 5),
        record=_Recorder(),
        max_rapid_restarts=2,
        healthy_run_s=60.0,
    )

    # Without the reset the third failure would be the third in a row and the
    # supervisor would have given up at four children instead of reaching five.
    assert verdict == sup.STOPPED
    assert len(made) == 5
    # The backoff resets with the budget: 1, 2, then 1 again after the healthy run.
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


def test_backoff_grows_and_then_caps():
    clock = _Clock()
    spawn, made = _spawner(clock, [(0.5, 1)])
    sleeps: list[float] = []

    sup.supervise(
        spawn=spawn,
        sleep=sleeps.append,
        clock=clock,
        should_continue=_stop_after(made, 10),
        record=_Recorder(),
        max_rapid_restarts=50,
    )

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


# --- state on disk --------------------------------------------------------


def test_state_merges_rather_than_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(sup, "_SUPERVISOR_STATE", str(tmp_path / "bridge.supervisor"))
    assert sup.read_supervisor_state() == {}

    sup.update_supervisor_state(pid=123, started=1.0)
    sup.update_supervisor_state(restarts=2)
    state = sup.read_supervisor_state()
    assert state == {"pid": 123, "started": 1.0, "restarts": 2}
    assert sup.supervisor_pid() == 123

    sup.clear_supervisor_state()
    assert sup.read_supervisor_state() == {}
    assert sup.supervisor_pid() is None


def test_state_survives_a_truncated_file(tmp_path, monkeypatch):
    path = tmp_path / "bridge.supervisor"
    path.write_text("{not json")
    monkeypatch.setattr(sup, "_SUPERVISOR_STATE", str(path))
    assert sup.read_supervisor_state() == {}  # never raises


def test_the_give_up_record_outlives_the_supervisor(tmp_path, monkeypatch):
    """The one outcome whose explanation has to survive the process.

    Everything else the supervisor knows is moot once it exits; a give-up is
    the only thing that can tell the next ``kiln bridge status`` why there is
    no bridge running.
    """
    path = tmp_path / "bridge.supervisor"
    monkeypatch.setattr(sup, "_SUPERVISOR_STATE", str(path))

    def gave_up(**_kwargs):
        sup.update_supervisor_state(gave_up=True, last_exit={"code": 1, "at": 9.0})
        return sup.GAVE_UP

    monkeypatch.setattr(sup, "supervise", gave_up)
    sup.run_supervisor()

    assert json.loads(path.read_text())["gave_up"] is True


def test_a_deliberate_stop_leaves_nothing_behind(tmp_path, monkeypatch):
    path = tmp_path / "bridge.supervisor"
    monkeypatch.setattr(sup, "_SUPERVISOR_STATE", str(path))
    monkeypatch.setattr(sup, "supervise", lambda **_kw: sup.STOPPED)

    sup.run_supervisor()

    assert not path.exists()
    assert sup.read_supervisor_state() == {}


# --- the signal path ------------------------------------------------------


def test_stopping_reaches_into_the_running_child():
    """``child.wait()`` blocks, so a handler that only sets a flag is a handler
    that is never noticed — the bridge's reconnect loop never exits on its own.
    """
    runner = sup._Runner()
    child = _Child(_Clock(), 1.0, 0)
    runner.track(child)

    assert runner.keep_going() is True
    runner.request_stop()
    assert runner.keep_going() is False
    assert child.terminated is True, "the child was left running after a stop"


def test_a_stop_that_lands_before_the_spawn_still_takes():
    """The race: stop arrives between the loop's check and the new child."""
    runner = sup._Runner()
    runner.request_stop()
    child = _Child(_Clock(), 1.0, 0)
    runner.track(child)
    assert child.terminated is True
