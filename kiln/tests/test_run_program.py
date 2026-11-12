"""Every program Kiln starts is started without fork() on macOS.

The launcher (:mod:`kiln.run_program`) is checked by behaviour: each launch
below runs a real child and watches the two ways :mod:`subprocess` can start
one, ``posix_spawn`` (recorded) and ``fork`` (refused).  The census at the end
proves every launch in Kiln goes through the launcher, and that the census
itself sees each spelling of a launch.
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kiln import run_program

_posixsubprocess = pytest.importorskip("_posixsubprocess", reason="POSIX launches only")

KILN_SRC = Path(run_program.__file__).resolve().parent


@pytest.fixture
def no_fork(monkeypatch):
    """Launch as on macOS, record each posix_spawn, refuse any fork."""
    if not getattr(subprocess, "_USE_POSIX_SPAWN", False):
        pytest.skip("this Python's subprocess cannot use posix_spawn here")
    monkeypatch.setattr(run_program, "_avoids_fork", lambda: True)
    spawned: list[str] = []
    real_spawn = os.posix_spawn

    def spawn(path, *args, **kwargs):
        spawned.append(os.fsdecode(path))
        return real_spawn(path, *args, **kwargs)

    def forked(*_args, **_kwargs):
        raise AssertionError("a launch forked")

    monkeypatch.setattr(os, "posix_spawn", spawn)
    # 3.12+ binds fork_exec into subprocess at import; 3.11 calls it on the module.
    monkeypatch.setattr(subprocess, "_fork_exec", forked, raising=False)
    monkeypatch.setattr(_posixsubprocess, "fork_exec", forked)
    return spawned


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_plain_launch_is_spawned(no_fork):
    result = run_program.run(_py("print('ran')"), capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "ran"
    assert no_fork == [sys.executable]


def test_a_bare_program_name_is_found_on_the_path(no_fork):
    result = run_program.run(["sh", "-c", "echo found"], capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "found"
    assert len(no_fork) == 1 and os.path.isabs(no_fork[0])


def test_a_working_directory_is_set_without_forking(no_fork, tmp_path):
    result = run_program.run(
        _py("import os; print(os.getcwd())"), cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)
    assert no_fork


def test_a_new_session_is_started_and_the_pid_is_the_programs(no_fork):
    proc = run_program.Popen(
        _py("import os; print(os.getpid(), os.getpgid(0))"),
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pid, group = proc.communicate(timeout=30)[0].split()
    assert int(pid) == proc.pid
    assert int(group) == proc.pid, "the program leads its own process group"
    assert no_fork


@pytest.mark.parametrize("how", ["direct", "cwd", "session"])
def test_sigpipe_is_back_to_default_in_the_program(no_fork, tmp_path, how):
    """``yes`` writing into a closed pipe dies of SIGPIPE only when the
    signal is at its default; Python ignores it, so a trampoline that forgot
    to put it back would leave ``yes`` exiting with an error instead."""
    extra = {"cwd": {"cwd": tmp_path}, "session": {"start_new_session": True}}.get(how, {})
    proc = run_program.Popen(["yes"], stdout=subprocess.PIPE, **extra)
    proc.stdout.read(16)
    proc.stdout.close()
    assert proc.wait(timeout=30) == -signal.SIGPIPE
    assert no_fork


def test_a_missing_program_raises_file_not_found(no_fork):
    with pytest.raises(FileNotFoundError):
        run_program.run(["kiln-no-such-program-anywhere"])
    assert no_fork == []


def test_a_missing_working_directory_raises_file_not_found(no_fork, tmp_path):
    with pytest.raises(FileNotFoundError):
        run_program.run(["true"], cwd=tmp_path / "gone")


def test_results_and_errors_name_the_command_as_given(no_fork, tmp_path):
    given = ["sh", "-c", "exit 3"]
    assert run_program.run(given, cwd=tmp_path).args == given
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_program.run(given, cwd=tmp_path, check=True)
    assert raised.value.cmd == given and raised.value.returncode == 3
    assert run_program.Popen(given, cwd=tmp_path).args == given


def test_a_stream_on_a_low_descriptor_is_handed_over(no_fork, capfd):
    run_program.run(_py("print('to fd 1')"), stdout=1, timeout=30)
    assert "to fd 1" in capfd.readouterr().out
    assert no_fork


def test_an_option_spawn_cannot_honour_is_left_as_given(monkeypatch):
    monkeypatch.setattr(run_program, "_avoids_fork", lambda: True)
    args, kwargs, opened = run_program._prepare(["true"], {"pass_fds": (5,)})
    assert (args, kwargs, opened) == (["true"], {"pass_fds": (5,)}, [])


def test_other_platforms_are_left_as_given(monkeypatch):
    monkeypatch.setattr(run_program, "_avoids_fork", lambda: False)
    kwargs = {"cwd": "/", "capture_output": True}
    assert run_program._prepare(["true"], kwargs) == (["true"], kwargs, [])


def test_the_slicer_launch_is_spawned(no_fork, tmp_path):
    """The door the crash report came through: the slicer's own launch."""
    from kiln.slicer import _run_slicer_with_startup_retry

    result = _run_slicer_with_startup_retry(
        _py("import os; print(os.getcwd())"),
        timeout=30,
        retryable_returncodes=frozenset({-11}),
        remove_partial_output=lambda: None,
        output_is_complete=lambda: False,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(tmp_path)
    assert no_fork


def test_open_url_on_macos_puts_the_spawned_open_first(monkeypatch):
    """The address goes to ``open`` started here, through webbrowser's own
    order, so a test or caller faking ``webbrowser.open`` still sees it."""
    import webbrowser

    monkeypatch.setattr(run_program, "_avoids_fork", lambda: True)
    monkeypatch.setattr(run_program, "_browser_registered", False)
    monkeypatch.setattr(webbrowser, "_tryorder", list(webbrowser._tryorder or []))
    monkeypatch.setattr(webbrowser, "_browsers", dict(webbrowser._browsers))
    monkeypatch.delenv("BROWSER", raising=False)
    seen = []

    def fake_run(args, **_kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(run_program, "run", fake_run)
    assert run_program.open_url("https://kiln3d.com") is True
    assert webbrowser._tryorder[0] == "kiln-open"
    assert seen == [["/usr/bin/open", "-u", "https://kiln3d.com"]]


@pytest.mark.parametrize("address", ["-a Calculator", "file:///etc/hosts", "/Applications/Calculator.app", "x-app://do"])
def test_open_declines_anything_but_a_web_address(monkeypatch, address):
    monkeypatch.setattr(run_program, "run", lambda *_a, **_k: pytest.fail("open was started"))
    assert run_program._hand_to_open(address) is False


def test_stderr_joins_the_captured_output_in_check_output(no_fork):
    """``check_output(stderr=STDOUT)`` merges the child's errors into what it
    returns; it must never send them to this process's own stdout."""
    out = run_program.check_output(
        _py("import sys; print('out'); print('err', file=sys.stderr)"), stderr=subprocess.STDOUT, text=True
    )
    assert "out" in out and "err" in out
    assert no_fork


def test_a_stand_in_for_popen_gets_its_object_back(monkeypatch):
    """A caller that swaps ``subprocess.Popen`` for a stand-in gets the
    stand-in's object, not an error from treating the stand-in as a type."""
    made = object()
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: made)
    assert run_program.Popen([sys.executable, "-c", "pass"]) is made


# -- census: every launch in Kiln goes through the launcher -------------------

_LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
_OS_LAUNCHERS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"} | {
    f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")
}
_ELSEWHERE = {
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "pty": {"fork", "spawn"},
    "webbrowser": {"open", "open_new", "open_new_tab"},
}
#: Vendored OpenSCAD library code that Kiln never imports.
_NOT_KILNS = ("data/scad_libraries/",)


def raw_launches(source: str) -> list[int]:
    """Lines of *source* that start a program without the launcher."""
    tree = ast.parse(source)
    subprocess_names = {"subprocess"}
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_names.add(alias.asname or "subprocess")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module in ("subprocess", "pty", "webbrowser")
            and any(a.name in _LAUNCHERS | {"fork", "spawn", "open", "open_new", "open_new_tab"} for a in node.names)
        ):
            found.append(node.lineno)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        if not isinstance(base, ast.Name):
            continue
        attr = node.func.attr
        if (
            (base.id in subprocess_names and attr in _LAUNCHERS)
            or (base.id == "os" and attr in _OS_LAUNCHERS)
            or attr in _ELSEWHERE.get(base.id, ())
        ):
            found.append(node.lineno)
    return sorted(found)


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['x'])",
        "import subprocess\nsubprocess.Popen(['x'])",
        "import subprocess\nsubprocess.check_output(['x'])",
        "def f():\n    import subprocess as _sp\n    _sp.run(['x'])",
        "from subprocess import Popen",
        "import os\nos.system('x')",
        "import os\nos.posix_spawn('x', ['x'], {})",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "import webbrowser\nwebbrowser.open('https://kiln3d.com')",
    ],
)
def test_the_census_sees_each_spelling_of_a_launch(source):
    assert raw_launches(source), source


def test_the_census_passes_the_launcher_and_constants():
    source = (
        "import subprocess\nfrom kiln import run_program\n"
        "run_program.run(['x'], stdout=subprocess.PIPE)\n"
        "try:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n"
    )
    assert raw_launches(source) == []


def test_every_launch_in_kiln_goes_through_the_launcher():
    offenders = []
    for path in sorted(KILN_SRC.rglob("*.py")):
        rel = path.relative_to(KILN_SRC).as_posix()
        if path.name == "run_program.py" or rel.startswith(_NOT_KILNS):
            continue
        offenders += [f"{rel}:{line}" for line in raw_launches(path.read_text(encoding="utf-8"))]
    assert offenders == [], (
        "These start a program without kiln.run_program, so on macOS they fork and can crash "
        "before the program runs: " + ", ".join(offenders)
    )
