"""``kiln.proc_alive.pid_alive`` answers for a live, a dead, and a strange pid.

The probe is shared by every store that remembers a holder's pid, so these
prove its contract on whichever platform runs the suite: a child that still
runs reads alive, a child that has exited reads dead, our own pid is alive
without asking, and anything that is not a positive integer is no process at
all.  The Windows branch's reading of ``OpenProcess`` failures is pinned with
a stand-in kernel, since no real pid produces access-denied on demand.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from kiln import proc_alive
from kiln.proc_alive import pid_alive


def test_a_live_child_reads_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()


def test_an_exited_child_reads_dead():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert pid_alive(child.pid) is False
    assert pid_alive(child.pid, unknown=True) is False


def test_our_own_pid_is_alive():
    assert pid_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, "7", True, None, 7.0])
def test_anything_but_a_positive_integer_is_no_process(pid):
    assert pid_alive(pid) is False
    assert pid_alive(pid, unknown=True) is False


class _Kernel:
    """A kernel32 whose ``OpenProcess`` fails the way the test says."""

    def __init__(self, opened: int = 0, exit_code: int | None = None, status_ok: bool = True):
        self.opened = opened
        self.exit_code = exit_code
        self.status_ok = status_ok
        self.closed: list[int] = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 -- the Windows name
        return self.opened

    def GetExitCodeProcess(self, handle, out):  # noqa: N802
        if self.exit_code is not None:
            out._obj.value = self.exit_code
        return 1 if self.status_ok else 0

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)


@pytest.mark.skipif(os.name != "nt", reason="the OpenProcess probe runs only on Windows")
class TestTheWindowsProbe:
    def _kernel(self, monkeypatch, kernel: _Kernel, error: int):
        monkeypatch.setattr(proc_alive, "_kernel32", lambda: kernel)
        monkeypatch.setattr(proc_alive, "_last_error", lambda: error)
        monkeypatch.setattr(proc_alive.os, "kill", lambda *_a: pytest.fail("signalled the process"))

    def test_access_denied_is_a_process(self, monkeypatch):
        self._kernel(monkeypatch, _Kernel(opened=0), error=5)
        assert pid_alive(4242) is True
        assert pid_alive(4242, unknown=False) is True

    def test_invalid_parameter_is_no_process(self, monkeypatch):
        self._kernel(monkeypatch, _Kernel(opened=0), error=87)
        assert pid_alive(4242) is False
        assert pid_alive(4242, unknown=True) is False

    def test_any_other_failure_is_the_callers_unknown(self, monkeypatch):
        self._kernel(monkeypatch, _Kernel(opened=0), error=1)
        assert pid_alive(4242, unknown=True) is True
        assert pid_alive(4242, unknown=False) is False

    def test_an_ended_process_someone_still_holds_reads_dead(self, monkeypatch):
        kernel = _Kernel(opened=11, exit_code=0)
        self._kernel(monkeypatch, kernel, error=0)
        assert pid_alive(4242) is False
        assert kernel.closed == [11]

    def test_still_active_reads_alive(self, monkeypatch):
        kernel = _Kernel(opened=11, exit_code=259)
        self._kernel(monkeypatch, kernel, error=0)
        assert pid_alive(4242, unknown=False) is True
        assert kernel.closed == [11]


@pytest.mark.skipif(os.name == "nt", reason="signal 0 is a probe only on POSIX")
class TestThePosixProbe:
    def test_a_process_we_may_not_signal_is_alive(self, monkeypatch):
        def _denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(proc_alive.os, "kill", _denied)
        assert pid_alive(4242, unknown=False) is True

    def test_an_unclassified_error_is_the_callers_unknown(self, monkeypatch):
        def _odd(pid, sig):
            raise OSError("odd")

        monkeypatch.setattr(proc_alive.os, "kill", _odd)
        assert pid_alive(4242, unknown=True) is True
        assert pid_alive(4242, unknown=False) is False
