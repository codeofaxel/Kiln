"""An install another program keeps up to date never replaces its own files.

When ``KILN_UPDATE_COMMAND`` names the command that updates this copy of Kiln
(set by the program that installed it), every door that would update Kiln in
place answers with that command instead, and nothing reaches pip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kiln import self_update, version_check

MANAGED = "hermes plugins update kiln"


def _runner_that_must_not_run(*args, **kwargs):
    raise AssertionError("pip ran on an install another program manages")


def test_unmanaged_install_has_no_managed_command(monkeypatch):
    monkeypatch.delenv(self_update.MANAGED_UPDATE_ENV, raising=False)
    assert self_update.managed_update_command() is None


def test_blank_value_is_not_managed(monkeypatch):
    monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, "   ")
    assert self_update.managed_update_command() is None


def test_perform_upgrade_refuses_and_names_the_command(monkeypatch):
    monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, MANAGED)
    out = self_update.perform_upgrade(runner=_runner_that_must_not_run, force=True)
    assert out["ok"] is False
    assert out["status"] == "managed"
    assert out["command"] == MANAGED
    assert MANAGED in out["message"]
    assert "pip" not in out["message"]


@pytest.fixture()
def upgrade_kiln():
    from kiln.plugins.utility_tools import _UtilityToolsPlugin

    tools: dict = {}

    class MockMCP:
        def tool(self, **_kwargs):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    _UtilityToolsPlugin().register(MockMCP())
    return tools["upgrade_kiln"]


def test_upgrade_tool_does_not_offer_to_update(monkeypatch, upgrade_kiln):
    monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, MANAGED)
    monkeypatch.setattr(self_update, "perform_upgrade", _runner_that_must_not_run)

    for confirm in (False, True):
        out = upgrade_kiln(confirm=confirm, force=True)
        assert out["status"] == "managed", out
        assert out["command"] == MANAGED
        assert "want me to update" not in out["message"].lower()


def test_cli_self_update_refuses_without_running_pip(monkeypatch):
    monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, MANAGED)
    import subprocess

    monkeypatch.setattr(subprocess, "call", _runner_that_must_not_run)
    from kiln.cli.main import cli

    result = CliRunner().invoke(cli, ["self-update", "--yes"])
    assert result.exit_code == 1, result.output
    assert MANAGED in result.output


def test_pypi_nudge_is_off_for_a_managed_install(monkeypatch, tmp_path):
    monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, MANAGED)
    monkeypatch.delenv("KILN_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("KILN_OFFLINE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = Path(tmp_path) / ".kiln" / "update_check.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"latest": "99.0.0", "checked_at": 9e12}))
    assert version_check.update_check_enabled() is False
    assert version_check.check_for_update("1.0.0") is None


@pytest.mark.parametrize("value", ["", None])
def test_unmanaged_install_still_checks(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(self_update.MANAGED_UPDATE_ENV, raising=False)
    else:
        monkeypatch.setenv(self_update.MANAGED_UPDATE_ENV, value)
    monkeypatch.delenv("KILN_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("KILN_OFFLINE", raising=False)
    assert version_check.update_check_enabled() is True
