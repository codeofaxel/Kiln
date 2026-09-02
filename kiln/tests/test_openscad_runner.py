"""Tests for the shared OpenSCAD runner and its startup-crash retry.

Two things are pinned here, and the second matters as much as the first:

1. The retry fires for the upstream startup crash and stays out of the way
   of everything else — above all, a real error in a user's SCAD, which
   must be reported the first time and not buried under two silent reruns.
2. Every OpenSCAD invocation in the package actually goes through the
   runner.  A shared helper nobody calls is the same bug with extra steps,
   and the only thing standing between this codebase and a nineteenth
   hand-rolled ``subprocess.run`` is a test that counts them.

A note on how the crash is simulated, because the obvious way is wrong.

Having a child really die of SIGSEGV is the most faithful test and it is
not worth what it costs: macOS files a diagnostic report and throws a
"Python quit unexpectedly" dialog for every genuine crash signal, so one
run of this file buried the developer's screen in popups.  The platform
contract and the policy are therefore pinned separately:

- one real child, killed with SIGTERM, pins the only platform fact the
  runner depends on — that a signal death reaches Python as a NEGATIVE
  return code.  SIGTERM is not a crash signal, so macOS stays quiet;
- the retry policy is driven through a fake :func:`subprocess.run` that
  returns chosen return codes and counts launches.  That is the layer the
  runner's decisions live at, and no process has to die to exercise it.
"""

from __future__ import annotations

import ast
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiln.openscad_runner import (
    OPENSCAD_CRASH_RETURNCODES,
    crashed_on_startup,
    run_openscad,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

_SEGV = -int(signal.SIGSEGV)
_BUS = -int(signal.SIGBUS)


# ---------------------------------------------------------------------------
# A fake OpenSCAD, installed at the subprocess.run seam
# ---------------------------------------------------------------------------

class FakeOpenSCAD:
    """Stands in for :func:`subprocess.run` and records every launch.

    :param returncodes: One entry per launch.  The final entry repeats
        forever, so ``[-11]`` means "crashes every time" and ``[-11, 0]``
        means "crashes once, then works".
    :param stderr: Text handed back as the child's stderr.
    :param writes: ``(path, content)`` written on each launch, before the
        return code is reported — the shape of a process that produced
        output and then died on the way out.
    :param duration: Seconds to burn per launch, for the late-crash rule.
    """

    def __init__(self, returncodes, *, stderr="", writes=None, duration=0.0):
        self._returncodes = list(returncodes)
        self._stderr = stderr
        self._writes = writes
        self._duration = duration
        self.launches = 0

    def __call__(self, argv, **kwargs):
        self.launches += 1
        if self._duration:
            time.sleep(self._duration)
        if self._writes:
            path, content = self._writes
            Path(path).write_text(content)

        index = min(self.launches - 1, len(self._returncodes) - 1)
        text = kwargs.get("text", True)
        blank = "" if text else b""
        stderr = self._stderr if text else self._stderr.encode()
        return subprocess.CompletedProcess(
            list(argv), self._returncodes[index], blank, stderr or blank,
        )


@pytest.fixture
def fake_run(monkeypatch):
    """Install a :class:`FakeOpenSCAD` in place of the runner's subprocess.run."""
    def install(fake: FakeOpenSCAD) -> FakeOpenSCAD:
        monkeypatch.setattr("kiln.openscad_runner.subprocess.run", fake)
        return fake

    return install


CMD = ["/fake/openscad", "-o", "out.stl", "in.scad"]


# ---------------------------------------------------------------------------
# The platform fact everything else rests on
# ---------------------------------------------------------------------------

class TestSignalDeathIsANegativeReturnCode:
    """If this is ever false, the whole crash set is meaningless.

    Uses SIGTERM deliberately: it proves the negative-return-code
    contract with a real process and a real signal, without tripping the
    macOS crash reporter the way SIGSEGV would.
    """

    def test_real_child_killed_by_signal_reports_negative(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.terminate()
        assert proc.wait(timeout=10) == -int(signal.SIGTERM)


# ---------------------------------------------------------------------------
# The retry itself
# ---------------------------------------------------------------------------

class TestStartupCrashRetries:
    """A signal death with nothing written is retried."""

    def test_crash_with_no_output_is_retried(self, fake_run):
        fake = fake_run(FakeOpenSCAD([_SEGV]))
        result = run_openscad(CMD, timeout=30, attempts=3)

        assert result.returncode == _SEGV
        assert fake.launches == 3, "every attempt should have been used"

    def test_retry_succeeds_and_returns_the_good_run(self, fake_run, tmp_path):
        """The realistic case: crash once, succeed on the retry."""
        out = tmp_path / "out.stl"
        fake = fake_run(FakeOpenSCAD([_SEGV, 0]))
        result = run_openscad(CMD, timeout=30, output_path=str(out))

        assert result.returncode == 0
        assert fake.launches == 2, "one crash, one successful retry"

    def test_two_crashes_then_success(self, fake_run):
        """Three attempts means two retries, not one."""
        fake = fake_run(FakeOpenSCAD([_SEGV, _SEGV, 0]))
        result = run_openscad(CMD, timeout=30, attempts=3)

        assert result.returncode == 0
        assert fake.launches == 3

    def test_sigbus_is_retried_too(self, fake_run):
        """Same fault class, same window, same treatment."""
        fake = fake_run(FakeOpenSCAD([_BUS]))
        result = run_openscad(CMD, timeout=30, attempts=2)

        assert result.returncode == _BUS
        assert fake.launches == 2

    def test_shell_convention_returncode_is_retried(self, fake_run):
        """128+N, carried for the same reason kiln.slicer carries it."""
        fake = fake_run(FakeOpenSCAD([128 + int(signal.SIGSEGV)]))
        run_openscad(CMD, timeout=30, attempts=2)

        assert fake.launches == 2

    def test_attempts_one_disables_retrying(self, fake_run):
        fake = fake_run(FakeOpenSCAD([_SEGV]))
        run_openscad(CMD, timeout=30, attempts=1)

        assert fake.launches == 1

    def test_retry_is_logged_not_silent(self, fake_run, caplog):
        """An invisible retry is an invisible bug — it must reach the log."""
        fake_run(FakeOpenSCAD([_SEGV]))
        with caplog.at_level(logging.WARNING, logger="kiln.openscad_runner"):
            run_openscad(CMD, timeout=30, attempts=2)

        messages = [r.getMessage() for r in caplog.records]
        assert any("retrying" in m.lower() for m in messages)
        assert any("SIGSEGV" in m for m in messages)

    def test_exhausted_retries_are_logged_as_error(self, fake_run, caplog):
        fake_run(FakeOpenSCAD([_SEGV]))
        with caplog.at_level(logging.ERROR, logger="kiln.openscad_runner"):
            run_openscad(CMD, timeout=30, attempts=2)

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "giving up must be visible, not just the retries"
        assert any("not a problem with the model" in m for m in errors)


class TestRealErrorsAreNotRetried:
    """The half that protects users from having their own bugs hidden."""

    def test_scad_syntax_error_is_not_retried(self, fake_run):
        """A genuine OpenSCAD error is an ANSWER. Retrying it hides it."""
        fake = fake_run(FakeOpenSCAD(
            [1], stderr="ERROR: Parser error: syntax error in file model.scad, line 3\n",
        ))
        result = run_openscad(CMD, timeout=30, attempts=3)

        assert result.returncode == 1
        assert "Parser error" in result.stderr
        assert fake.launches == 1, "a real SCAD error must run exactly once"

    def test_success_runs_once(self, fake_run):
        fake = fake_run(FakeOpenSCAD([0]))
        result = run_openscad(CMD, timeout=30, attempts=3)

        assert result.returncode == 0
        assert fake.launches == 1

    def test_sigabrt_is_not_retried(self, fake_run):
        """An abort is how OpenSCAD reports a real failure on real geometry."""
        fake = fake_run(FakeOpenSCAD([-int(signal.SIGABRT)]))
        run_openscad(CMD, timeout=30, attempts=3)

        assert fake.launches == 1

    def test_sigkill_is_not_retried(self, fake_run):
        """SIGKILL is the OOM killer or a human. Neither wants a rerun."""
        fake = fake_run(FakeOpenSCAD([-int(signal.SIGKILL)]))
        run_openscad(CMD, timeout=30, attempts=3)

        assert fake.launches == 1

    def test_crash_after_writing_output_is_salvaged(self, fake_run, tmp_path):
        """Died on the way out, with the file already written.

        The same call kiln.slicer makes for OrcaSlicer: keep the output
        and let the caller judge it, rather than throwing away a good
        result because of how the process happened to exit.
        """
        out = tmp_path / "out.stl"
        fake = fake_run(FakeOpenSCAD(
            [_SEGV], writes=(str(out), "solid kiln\nendsolid kiln\n"),
        ))
        result = run_openscad(CMD, timeout=30, output_path=str(out), attempts=3)

        assert result.returncode == _SEGV
        assert fake.launches == 1, "output was written — nothing to retry"
        assert out.read_text().startswith("solid")

    def test_empty_output_file_does_not_count_as_written(self, fake_run, tmp_path):
        """mkstemp pre-creates the target; zero bytes is not a result."""
        out = tmp_path / "out.stl"
        out.touch()
        fake = fake_run(FakeOpenSCAD([_SEGV]))
        run_openscad(CMD, timeout=30, output_path=str(out), attempts=2)

        assert fake.launches == 2

    def test_late_crash_is_not_retried(self, fake_run, monkeypatch):
        """A segfault deep in a boolean is not the startup race.

        Retrying it would make the user wait out the same doomed
        computation three times to be told the same thing.
        """
        monkeypatch.setattr("kiln.openscad_runner._STARTUP_CRASH_WINDOW_S", 0.05)
        fake = fake_run(FakeOpenSCAD([_SEGV], duration=0.2))
        run_openscad(CMD, timeout=30, attempts=3)

        assert fake.launches == 1


class TestRunnerContract:
    """The bits call sites depend on."""

    def test_timeout_propagates(self, fake_run):
        """Every call site already handles TimeoutExpired; don't swallow it."""
        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 0.5)

        fake_run(boom)
        with pytest.raises(subprocess.TimeoutExpired):
            run_openscad(CMD, timeout=0.5)

    def test_missing_binary_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            run_openscad([str(tmp_path / "does_not_exist")], timeout=5)

    def test_output_is_always_captured(self, fake_run):
        captured = {}

        def spy(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        fake_run(spy)
        run_openscad(CMD, timeout=30)

        assert captured.get("capture_output") is True

    def test_text_false_is_passed_through(self, fake_run):
        fake = fake_run(FakeOpenSCAD([0]))
        result = run_openscad(CMD, timeout=30, text=False)

        assert isinstance(result.stdout, bytes)
        assert fake.launches == 1

    def test_cwd_and_timeout_are_passed_through(self, fake_run):
        captured = {}

        def spy(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        fake_run(spy)
        run_openscad(CMD, timeout=17, cwd="/tmp")

        assert captured["timeout"] == 17
        assert captured["cwd"] == "/tmp"

    def test_retry_gets_a_fresh_timeout(self, fake_run):
        """A crash inside the startup window consumed none of the budget."""
        seen = []

        def spy(argv, **kwargs):
            seen.append(kwargs["timeout"])
            return subprocess.CompletedProcess(argv, _SEGV, "", "")

        fake_run(spy)
        run_openscad(CMD, timeout=60, attempts=3)

        assert seen == [60, 60, 60]

    def test_crashed_on_startup_classifies_return_codes(self):
        assert crashed_on_startup(subprocess.CompletedProcess([], _SEGV))
        assert crashed_on_startup(subprocess.CompletedProcess([], _BUS))
        assert not crashed_on_startup(subprocess.CompletedProcess([], 0))
        assert not crashed_on_startup(subprocess.CompletedProcess([], 1))
        assert not crashed_on_startup(
            subprocess.CompletedProcess([], -int(signal.SIGKILL))
        )

    def test_sigsegv_is_in_the_crash_set(self):
        """Matches kiln.slicer._ORCA_SIGSEGV_RETURNCODES, the precedent."""
        assert _SEGV in OPENSCAD_CRASH_RETURNCODES
        assert 128 + int(signal.SIGSEGV) in OPENSCAD_CRASH_RETURNCODES

    def test_abort_and_kill_are_not_in_the_crash_set(self):
        assert -int(signal.SIGABRT) not in OPENSCAD_CRASH_RETURNCODES
        assert -int(signal.SIGKILL) not in OPENSCAD_CRASH_RETURNCODES


# ---------------------------------------------------------------------------
# The door count: every OpenSCAD launch goes through the runner
# ---------------------------------------------------------------------------

#: Every module in the package that launches an OpenSCAD binary.
#:
#: Each is required to hold ZERO direct ``subprocess`` launches: as of
#: this test's writing every subprocess call in each of these modules is
#: an OpenSCAD launch, so the rule can be "none at all" rather than a
#: guess about which argv is which.  Adding a non-OpenSCAD subprocess
#: call to one of these will fail this test — at which point tighten the
#: check, don't delete it.
_OPENSCAD_MODULES = (
    "decoration_helpers.py",
    "design_reasoning.py",
    "emboss_generator.py",
    "generation/gemini.py",
    "generation/openscad.py",
    "model_visualizer.py",
    "multicolor_3mf.py",
)

_LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}


def _subprocess_launches(path: Path) -> list[int]:
    """Line numbers of direct ``subprocess.<launcher>(...)`` calls in *path*."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _LAUNCHERS
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            hits.append(node.lineno)
    return hits


class TestEveryDoorRoutesThroughTheRunner:
    """The retry is worthless at any site that skipped it."""

    @pytest.mark.parametrize("module", _OPENSCAD_MODULES)
    def test_no_direct_subprocess_launch(self, module):
        path = _SRC / module
        assert path.is_file(), f"{module} moved — update _OPENSCAD_MODULES"

        hits = _subprocess_launches(path)
        assert not hits, (
            f"{module} launches a subprocess directly at line(s) {hits}. "
            f"OpenSCAD must be launched through "
            f"kiln.openscad_runner.run_openscad so the startup-crash retry "
            f"applies; see the module docstring there."
        )

    @pytest.mark.parametrize("module", _OPENSCAD_MODULES)
    def test_module_actually_calls_the_runner(self, module):
        """The other half: not just 'no subprocess', but 'yes runner'."""
        source = (_SRC / module).read_text()
        assert "run_openscad" in source, (
            f"{module} is listed as an OpenSCAD invocation site but never "
            f"calls run_openscad — either it stopped launching OpenSCAD "
            f"(drop it from _OPENSCAD_MODULES) or a call site was missed."
        )

    def test_no_unlisted_module_launches_openscad(self):
        """Catch a NEW module that starts launching OpenSCAD elsewhere.

        Heuristic but load-bearing: a subprocess launch whose argv begins
        with a variable named like an OpenSCAD binary, in a module not on
        the list.  This is the check that fires when someone adds site
        nineteen in a file nobody thought to look at.
        """
        suspicious = {"openscad", "openscad_binary", "binary", "exe", "scad_binary"}
        offenders: list[str] = []

        for path in sorted(_SRC.rglob("*.py")):
            rel = path.relative_to(_SRC).as_posix()
            if rel in _OPENSCAD_MODULES or "scad_libraries" in rel:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr in _LAUNCHERS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                ):
                    continue
                if not node.args:
                    continue
                argv = node.args[0]
                first = argv.elts[0] if isinstance(argv, ast.List) and argv.elts else argv
                name = ""
                if isinstance(first, ast.Name):
                    name = first.id
                elif isinstance(first, ast.Attribute):
                    name = first.attr
                if name.lower().lstrip("_") in suspicious:
                    offenders.append(f"{rel}:{node.lineno} (argv[0]={name})")

        assert not offenders, (
            "These look like OpenSCAD launches outside the shared runner:\n  "
            + "\n  ".join(offenders)
            + "\nRoute them through kiln.openscad_runner.run_openscad, or add "
            "the module to _OPENSCAD_MODULES if every launch in it is OpenSCAD."
        )


class TestLocaleHypothesisNotShipped:
    """Guards a claim, not a behaviour.

    Pinning ``LC_ALL=C`` for the child is the obvious-sounding fix for a
    locale race, and the attempt to test it failed rather than the idea:
    400 launches per arm, zero crashes on both sides, on a probe whose
    warm code-signature cache probably shut the race window entirely.
    If someone later ships it, it should arrive with evidence and a
    deliberate deletion of this test, not by drive-by.
    """

    def test_runner_does_not_override_child_locale(self, fake_run, monkeypatch):
        captured = {}

        def spy(argv, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, "", "")

        fake_run(spy)
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        run_openscad(CMD, timeout=30)

        env = captured.get("env")
        assert env is None or env.get("LC_ALL") == "en_US.UTF-8", (
            "The runner is overriding the child locale. That was measured "
            "and not demonstrated to help — see the module docstring."
        )
        assert os.environ["LC_ALL"] == "en_US.UTF-8"


# ---------------------------------------------------------------------------
# Windows: the crash sets must build on a signal module without SIGBUS
# ---------------------------------------------------------------------------

#: The Unix-only signals the crash sets name.  CPython's ``signal`` module
#: on Windows defines neither, so any module that reads them at import
#: time cannot be imported there at all.
_UNIX_ONLY_SIGNALS = ("SIGBUS", "SIGTRAP")

_WINDOWS_SHAPED_IMPORT = f"""
import json, signal
for name in {_UNIX_ONLY_SIGNALS!r}:
    delattr(signal, name)
import kiln.cli.main
import kiln.openscad_runner as runner
import kiln.slicer as slicer
print(json.dumps({{
    "openscad": sorted(runner.OPENSCAD_CRASH_RETURNCODES),
    "slic3r": sorted(slicer._SLIC3R_STARTUP_CRASH_RETURNCODES),
    "orca": sorted(slicer._ORCA_STARTUP_CRASH_RETURNCODES),
}}))
"""


class TestImportsOnWindowsShapedSignalModule:
    """Issue #146: ``kiln`` could not start on Windows.

    ``signal.SIGBUS`` is Unix-only and was read at module import time, so
    every entrypoint — ``kiln serve``, ``kiln doctor``, ``kiln --help`` —
    died with ``AttributeError`` before running a command.  ``kiln.slicer``
    carried the same fault twice over (``SIGTRAP`` and ``SIGBUS``), one
    lazy import further down the same road.

    The Windows signal module is simulated in a child interpreter by
    deleting the two attributes before the package is imported: that is
    exactly the shape of the failure, and nothing in this process has to
    be reloaded.  The sets are expected to degrade to what remains,
    ``SIGSEGV``, not to vanish.
    """

    def test_every_entrypoint_imports_and_the_sets_degrade(self):
        proc = subprocess.run(
            [sys.executable, "-c", _WINDOWS_SHAPED_IMPORT],
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ,
        )
        assert proc.returncode == 0, (
            "import failed on a signal module without SIGBUS/SIGTRAP:\n"
            + proc.stderr[-2000:]
        )
        import json

        sets = json.loads(proc.stdout.strip().splitlines()[-1])
        segv = int(signal.SIGSEGV)
        assert sets["openscad"] == sorted({-segv, 128 + segv})
        assert sets["slic3r"] == [-segv]
        assert sets["orca"] == []
