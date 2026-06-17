"""Tests for the CLI Terms-of-Use gate + the ``kiln accept-terms`` command.

The gate lives in the ``cli`` group callback and is one-time: once acceptance is
recorded, ``is_current()`` short-circuits and it never prompts again.  Exempt
commands (setup / accept-terms / serve / --help) must run before acceptance.
"""

from __future__ import annotations

from unittest import mock

import pytest
from click.testing import CliRunner

import kiln.cli.main as cli_main
from kiln.cli.main import _enforce_terms_gate, cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _quiet_startup(monkeypatch):
    """Silence the group callback's background update check (no network in tests)."""
    monkeypatch.setattr("kiln.version_check.kick_background_check", lambda: None, raising=False)
    monkeypatch.setattr("kiln.version_check.update_banner_line", lambda: "", raising=False)


# --- _enforce_terms_gate: the three paths ----------------------------------


def test_env_flag_records_by_configuration(monkeypatch):
    monkeypatch.setenv("KILN_ACCEPT_TERMS", "1")
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    _enforce_terms_gate()  # returns without raising
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("method") == "env"


def test_non_interactive_without_flag_exits_loudly(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()
    assert exc.value.code == 1


def test_interactive_accept_returns(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", lambda method="setup": True)
    _enforce_terms_gate()  # accepted -> no raise


def test_interactive_decline_exits(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", lambda method="setup": False)
    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()
    assert exc.value.code == 1


# --- the gate is wired into the group callback -----------------------------


def test_gate_fires_for_non_exempt_command(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    gate = mock.MagicMock(side_effect=SystemExit(1))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["self-update"])
    assert res.exit_code == 1
    gate.assert_called_once()


def test_gate_skipped_for_exempt_command(runner, monkeypatch):
    # accept-terms is exempt: the gate must NOT fire even when not current.
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    gate = mock.MagicMock(side_effect=AssertionError("gate fired for exempt command"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["accept-terms"])
    assert res.exit_code == 0
    gate.assert_not_called()


def test_gate_skipped_for_help(runner, monkeypatch):
    gate = mock.MagicMock(side_effect=AssertionError("gate fired on --help"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["--help"])
    assert res.exit_code == 0
    gate.assert_not_called()


# --- the `kiln accept-terms` command ---------------------------------------


def test_accept_terms_already_accepted(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    res = runner.invoke(cli, ["accept-terms"])
    assert res.exit_code == 0
    assert "already accepted" in res.output.lower()


def test_accept_terms_yes_records_noninteractive(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    res = runner.invoke(cli, ["accept-terms", "--yes"])
    assert res.exit_code == 0
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("method") == "cli_noninteractive"
    assert "accepted" in res.output.lower()
