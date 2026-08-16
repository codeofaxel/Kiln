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

from unittest import mock

import pytest

from kiln import heartbeat


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

    Measured 2026-08-15: a linux row with zero printers, ``pro_installed``
    false, ``prints_today=5`` and a tool call literally named ``test``
    reached production — and was the ONLY content in the usage
    dashboard's paywall-demand panel, so an empty funnel read as real
    customer demand.  A container does not inherit the runner's ``CI``
    variables, so the env list could not see it; each run also mints a
    fresh ``installation_id``, inflating install counts with phantoms.
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
