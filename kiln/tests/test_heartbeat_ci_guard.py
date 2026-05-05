"""CI-runner guard for the daily-usage heartbeat.

Background: every CI job (GitHub Actions, etc.) gets a fresh ``$HOME``,
so ``~/.kiln/installation_id`` is regenerated on every invocation.  If
the heartbeat fires from CI, each job creates a new "install" — the
founder dashboard ended up showing 462 active installs in 30 days when
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
