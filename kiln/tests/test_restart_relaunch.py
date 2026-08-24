"""restart_server's exec target — the wrapper when one launched us.

The 2026-08-19 near-miss: an operator launcher (sync two runtime clones,
heal editable-install metadata, then serve) launched the server, and
restart_server re-exec'd ``python -m kiln serve`` directly — no sync, no
heal — while its docstring promised "picking up any code changes".  A
fix-verification almost graded OLD code because the tool whose whole
purpose is freshness structurally could not deliver it behind a wrapper.

The tool itself is never CALLED here — it spawns a thread that execs
over the current process, i.e. over pytest.  The helper carries the
decision, so the helper is tested; source pins prove the tool consumes
it rather than keeping a private second opinion.
"""

from __future__ import annotations

import inspect
import stat
import sys
from pathlib import Path

import pytest

from kiln import server

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="executable-bit checks are POSIX"
)


def _wrapper_file(tmp_path: Path, executable: bool = True) -> str:
    w = tmp_path / "kiln-serve"
    w.write_text("#!/bin/sh\nexec true\n")
    if executable:
        w.chmod(w.stat().st_mode | stat.S_IXUSR)
    return str(w)


def test_a_wrapper_that_announced_itself_is_re_entered(tmp_path: Path) -> None:
    w = _wrapper_file(tmp_path)
    argv, relaunch, note = server._restart_exec_target(
        {server._SERVE_WRAPPER_ENV: w}
    )
    assert argv == [w], "the restart must re-enter the launcher, not python"
    assert relaunch == "wrapper"
    assert w in note and "sync" in note


def test_no_wrapper_keeps_the_plain_install_restart() -> None:
    argv, relaunch, note = server._restart_exec_target({})
    assert argv == [sys.executable, "-m", "kiln", "serve"], (
        "a plain install's restart is a fresh python over the same resolve"
    )
    assert relaunch == "in-place"
    assert "no launcher logic" in note


def test_a_vanished_wrapper_falls_back_and_says_so(tmp_path: Path) -> None:
    """Silence here would be the stale-restart bug wearing a new coat:
    the env promises a launcher, the launcher is gone, and a quiet
    in-place exec would again LOOK like a code refresh."""
    gone = str(tmp_path / "not-there")
    argv, relaunch, note = server._restart_exec_target(
        {server._SERVE_WRAPPER_ENV: gone}
    )
    assert argv[0] == sys.executable
    assert relaunch == "in-place"
    assert gone in note and "missing or not executable" in note

    unexec = _wrapper_file(tmp_path, executable=False)
    argv2, relaunch2, note2 = server._restart_exec_target(
        {server._SERVE_WRAPPER_ENV: unexec}
    )
    assert argv2[0] == sys.executable and relaunch2 == "in-place"


def test_the_tool_consumes_the_helper_not_a_second_opinion() -> None:
    """Source pins: the exec uses the helper's argv, the result carries the
    machine verdict, and no hardcoded python exec remains to drift."""
    src = inspect.getsource(server.restart_server)
    assert "_restart_exec_target(" in src, (
        "restart_server no longer asks the helper where to exec"
    )
    assert "os.execve(argv[0], argv, new_env)" in src, (
        "the exec no longer uses the helper's answer"
    )
    assert '"relaunch": relaunch' in src, (
        "the result dropped the relaunch verdict — a caller can no longer "
        "tell a wrapper relaunch from an in-place re-exec"
    )
    assert '[sys.executable, "-m", "kiln", "serve"], new_env' not in src, (
        "a hardcoded python exec crept back beside the helper"
    )


def test_the_helper_reads_the_env_the_launcher_documents(tmp_path: Path) -> None:
    """The pairing is a NAME shared across two codebases (this module and
    an operator-owned shell script).  Pin the name so a rename here
    silently orphaning every launcher is caught as a failure instead."""
    assert server._SERVE_WRAPPER_ENV == "KILN_SERVE_WRAPPER"
