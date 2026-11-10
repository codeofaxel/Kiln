"""CI-runner guard for the daily-usage heartbeat.

Background: every CI job (GitHub Actions, etc.) gets a fresh ``$HOME``,
so ``~/.kiln/installation_id`` is regenerated on every invocation.  If
the heartbeat fires from CI, each job creates a new "install" — the
usage dashboard ended up showing 462 active installs in 30 days when
the real number was ~4, with the rest being CI runners.  The guard
short-circuits ``_send_heartbeat`` and ``send_heartbeat_async`` when
any well-known CI / build / test env var is set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from kiln import daily_stats, heartbeat
from kiln import dev_home_guard as guard
from kiln.runtime_env import HOSTED_ENV_VAR


@pytest.mark.parametrize(
    "env_var",
    [
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "CIRCLECI",
        "TRAVIS",
        "BUILDKITE",
        "JENKINS_URL",
        "TF_BUILD",
        "RUNNER_OS",
        "PYTEST_CURRENT_TEST",
    ],
)
def test_send_heartbeat_short_circuits_under_ci(env_var: str) -> None:
    """Each known CI/build/test env var, on its own, must suppress the ping."""
    with mock.patch.dict("os.environ", {env_var: "1"}, clear=False):
        with mock.patch("urllib.request.urlopen") as urlopen:
            heartbeat._send_heartbeat()
            urlopen.assert_not_called()


def test_send_heartbeat_async_short_circuits_under_ci() -> None:
    """The async entry point must not even spawn the daemon thread."""
    with mock.patch.dict("os.environ", {"CI": "true"}, clear=False):
        with mock.patch("threading.Thread") as Thread:
            heartbeat.send_heartbeat_async()
            Thread.assert_not_called()


def test_is_ci_environment_false_with_no_ci_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: with all known CI env vars cleared, the guard returns False."""
    for name in heartbeat._CI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert heartbeat._is_ci_environment() is False


# ---------------------------------------------------------------------------
# Containers — the runner the CI env list cannot see
# ---------------------------------------------------------------------------


class TestContainerIsNotAnInstall:
    """A container is an ephemeral runner, never a user's install.

    A container does not inherit the runner's ``CI`` variables, so it is
    recognised directly and its heartbeat is not counted as a user install.
    """

    def _no_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in heartbeat._CI_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(heartbeat._CONTAINER_OPT_IN, raising=False)

    def test_dockerenv_marks_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_ci(monkeypatch)
        monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
        assert heartbeat._is_container() is True
        assert heartbeat._is_ephemeral_runner() is True

    def test_cgroup_marks_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_ci(monkeypatch)
        monkeypatch.setattr("os.path.exists", lambda _p: False)
        monkeypatch.setattr(
            "builtins.open",
            lambda *a, **k: mock.mock_open(read_data="0::/kubepods/pod123")(),
        )
        assert heartbeat._is_container() is True

    def test_a_bare_host_is_not_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_ci(monkeypatch)
        monkeypatch.setattr("os.path.exists", lambda _p: False)

        def _no_proc(*_a, **_k):
            raise OSError("no /proc here")

        monkeypatch.setattr("builtins.open", _no_proc)
        assert heartbeat._is_container() is False
        assert heartbeat._is_ephemeral_runner() is False

    def test_the_opt_in_returns_a_real_containerised_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Someone whose durable install genuinely runs in a container."""
        self._no_ci(monkeypatch)
        monkeypatch.setenv(heartbeat._CONTAINER_OPT_IN, "1")
        monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
        assert heartbeat._is_container() is False

    def test_a_container_neither_sends_nor_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both halves of the surface, from one predicate."""
        self._no_ci(monkeypatch)
        monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
        with mock.patch("urllib.request.urlopen") as urlopen:
            heartbeat._send_heartbeat()
            urlopen.assert_not_called()

        from kiln import daily_stats

        # The recording side asks the same question — but only for the
        # default path; a test that repoints _STATS_PATH is deliberately
        # exercising recording and stays unsuppressed.
        monkeypatch.setattr(daily_stats, "_STATS_PATH", daily_stats._DEFAULT_STATS_PATH)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.setitem(__import__("sys").modules, "pytest", None)
        assert daily_stats._recording_suppressed() is True


# ---------------------------------------------------------------------------
# Sandboxed runs — a scratch home is nobody's install
# ---------------------------------------------------------------------------

SRC = Path(guard.__file__).resolve().parents[1]

#: The two marks the home guard gives a sandboxed run: one it was asked for,
#: and the one a moved run hands its children.
_SANDBOX_MARKS = [
    pytest.param({guard.SANDBOX_ENV: "1"}, id="asked"),
    pytest.param({guard.MOVED_ENV: os.path.join(os.sep, "scratch", "kiln-sandbox-home-x")}, id="child-of-a-moved-run"),
]


class _Ok:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestASandboxIsNotAnInstall:
    """A run the home guard calls a sandbox never sends the heartbeat, from
    any door, and records none of the counters it would carry.  The same
    host without the mark sends, so each refusal is the sandbox's."""

    @pytest.fixture
    def plain_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        """Every door open: no CI, no container, not hosted, telemetry on,
        nothing sent today.  ``wire`` is every URL a send reached.  The CI
        check is answered rather than its variables cleared: pytest sets
        ``PYTEST_CURRENT_TEST`` again when the test body starts."""
        for name in (guard.SANDBOX_ENV, guard.MOVED_ENV, HOSTED_ENV_VAR):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(heartbeat, "_is_ci_environment", lambda: False)
        monkeypatch.setattr(heartbeat, "_is_container", lambda: False)
        monkeypatch.setattr(heartbeat, "_telemetry_enabled", lambda: True)
        monkeypatch.setattr(heartbeat, "_sent_on", None)
        monkeypatch.setattr(heartbeat, "_scheduler_started", False)
        monkeypatch.setattr(heartbeat, "_LAST_BEAT_PATH", tmp_path / ".last_heartbeat")
        seen = SimpleNamespace(wire=[])

        def _urlopen(req, timeout=None):
            seen.wire.append(req.full_url)
            return _Ok()

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        return seen

    @pytest.mark.parametrize(
        ("mark", "threads", "beats"),
        [pytest.param({}, ["kiln-heartbeat", "kiln-heartbeat"], 1, id="plain"),
         *(pytest.param(p.values[0], [], 0, id=p.id) for p in _SANDBOX_MARKS)],
    )
    def test_every_door_beats_for_a_plain_host_and_none_for_a_sandbox(
        self, plain_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
        mark: dict[str, str], threads: list[str], beats: int,
    ) -> None:
        for name, value in mark.items():
            monkeypatch.setenv(name, value)
        started: list[str] = []
        with mock.patch.object(
            heartbeat.threading, "Thread", lambda *a, **kw: started.append(kw.get("name")) or mock.Mock()
        ):
            heartbeat.send_heartbeat_async()  # every kiln command
            heartbeat.start_heartbeat_scheduler()  # kiln serve
        heartbeat._send_heartbeat()  # the sender itself
        assert started == threads
        assert sum(url.endswith("/rpc/record_heartbeat") for url in plain_host.wire) == beats
        if not beats:
            assert plain_host.wire == []

    def test_a_sandbox_records_no_counters(
        self, plain_host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real file, and none of the runner's own marks: what is left to
        # refuse the write is the sandbox, or nothing.
        monkeypatch.setattr(daily_stats, "_STATS_PATH", daily_stats._DEFAULT_STATS_PATH)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
        assert daily_stats._recording_suppressed() is False
        monkeypatch.setenv(guard.SANDBOX_ENV, "1")
        assert daily_stats._recording_suppressed() is True

    def test_the_guard_reads_its_own_marks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(guard.SANDBOX_ENV, raising=False)
        monkeypatch.delenv(guard.MOVED_ENV, raising=False)
        assert guard.sandboxed() is False
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv(guard.SANDBOX_ENV, value)
            assert guard.sandboxed() is True
        monkeypatch.setenv(guard.SANDBOX_ENV, "0")
        assert guard.sandboxed() is False
        monkeypatch.setenv(guard.MOVED_ENV, os.path.join(os.sep, "scratch", "kiln-sandbox-home-x"))
        assert guard.sandboxed() is True


#: A fresh interpreter whose account home is the stand-in HOME it was given,
#: on a sealed network, runs the doors a ``kiln`` command and the sender use,
#: and prints every URL a send reached and where its home ended up.
_CHILD = r'''
import json, os, pwd, socket, sys, threading, urllib.request

class _Account:
    pw_dir = os.environ["HOME"]

pwd.getpwuid = lambda uid: _Account
sys.modules["kiln_pro"] = None

def _sealed(*a, **k):
    raise OSError("no network in this test")

socket.create_connection = _sealed
socket.socket.connect = _sealed
socket.getaddrinfo = _sealed
wire = []

class _Ok:
    status = 200
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _urlopen(req, timeout=None):
    wire.append(getattr(req, "full_url", str(req)))
    return _Ok()

urllib.request.urlopen = _urlopen

import kiln
from kiln import heartbeat

heartbeat.send_heartbeat_async()
for t in threading.enumerate():
    if t.name == "kiln-heartbeat":
        t.join(60)
heartbeat._send_heartbeat()
print(json.dumps({"wire": wire, "home": os.environ["HOME"], "moved": os.environ.get("KILN_SANDBOX_HOME")}))
'''


def _beat_in_a_child(home: Path, **env_over: str) -> dict:
    drop = {*heartbeat._CI_ENV_VARS, guard.SANDBOX_ENV, guard.ALLOW_REAL_HOME_ENV, guard.MOVED_ENV,
            HOSTED_ENV_VAR, "KILN_HOME", "KILN_TELEMETRY", "KILN_AUTH_HOME"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    # The scratch folders a move makes go under the stand-in's own temp
    # folder, so the stand-in reads as the account's real home, not scratch.
    temp = home / "tmp"
    temp.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(temp), PYTHONPATH=str(SRC),
               KILN_TELEMETRY_ALLOW_CONTAINER="1")
    env.update(env_over)
    out = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in home needs pwd")
class TestASandboxedProcess:
    """The whole path a ``kiln`` command takes, in a real child process: the
    home guard at import, then the heartbeat's door and its sender."""

    def test_a_plain_run_beats_once(self, tmp_path: Path) -> None:
        said = _beat_in_a_child(tmp_path)
        assert said["home"] == str(tmp_path) and said["moved"] is None
        assert [url for url in said["wire"] if url.endswith("/rpc/record_heartbeat")] == [
            f"{heartbeat._SUPABASE_URL}/rest/v1/rpc/record_heartbeat"
        ]

    def test_a_sandboxed_run_is_moved_and_sends_nothing(self, tmp_path: Path) -> None:
        said = _beat_in_a_child(tmp_path, **{guard.SANDBOX_ENV: "1"})
        assert "kiln-sandbox-home-" in said["home"] and said["moved"] == said["home"]
        assert said["wire"] == []

    def test_a_child_of_a_moved_run_sends_nothing(self, tmp_path: Path) -> None:
        moved = tmp_path / "tmp" / "kiln-sandbox-home-parent"
        (moved / ".kiln").mkdir(parents=True)
        said = _beat_in_a_child(tmp_path, HOME=str(moved), **{guard.MOVED_ENV: str(moved)})
        assert said["home"] == str(moved)
        assert said["wire"] == []

    def test_a_sandbox_on_the_real_records_sends_nothing_either(self, tmp_path: Path) -> None:
        said = _beat_in_a_child(tmp_path, **{guard.SANDBOX_ENV: "1", guard.ALLOW_REAL_HOME_ENV: "1"})
        assert said["home"] == str(tmp_path) and said["moved"] is None
        assert said["wire"] == []
